"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import styles from "./DataInspector.module.css";

type StoreSnap = {
  user_id: string;
  fetched_at: string;
  profile: Record<string, unknown>;
  people: Record<string, unknown>[];
  priorities: Record<string, unknown>[];
  usuals: Record<string, unknown>[];
  reminders: Record<string, unknown>[];
  contacts: Record<string, unknown>[];
  agenda: {
    total: number;
    level: number;
    recent: Record<string, unknown>[];
    level_recent: Record<string, unknown>[];
  };
  chat_turns: Record<string, unknown>[];
  negatives: Record<string, unknown>[];
};

type Mutation = { at: string; text: string };

const OPEN_KEY = "level.demo.inspector";
const WIDTH_KEY = "level.demo.inspector.width";
const POLL_MS = 2000;
// Sidebar resize bounds. 288px covers all the section labels without
// wrapping. 720px is roughly a full column of caregiver-length JSON
// blobs; beyond that the inspector starts stealing too much room
// from the main content on typical laptops.
const MIN_WIDTH_PX = 288;
const MAX_WIDTH_PX = 720;
const DEFAULT_WIDTH_PX = 352; // matches the previous 22rem default at root:16px

function labelOf(item: Record<string, unknown>, fallback: string): string {
  const v =
    item.summary ??
    item.text ??
    item.display_name ??
    item.display_summary ??
    item.title ??
    item.name ??
    fallback;
  const s = String(v);
  return s.length > 42 ? `${s.slice(0, 40)}…` : s;
}

function ids(items: Record<string, unknown>[], key: string): Map<string, Record<string, unknown>> {
  const map = new Map<string, Record<string, unknown>>();
  for (const item of items) {
    const id = String(item[key] ?? "");
    if (id) map.set(id, item);
  }
  return map;
}

function stable(value: unknown): string {
  return JSON.stringify(value ?? null);
}

function diffSnaps(prev: StoreSnap, next: StoreSnap): string[] {
  const lines: string[] = [];
  const profileKeys = new Set([...Object.keys(prev.profile), ...Object.keys(next.profile)]);
  for (const key of profileKeys) {
    if (stable(prev.profile[key]) === stable(next.profile[key])) continue;
    if (next.profile[key] == null || next.profile[key] === "") {
      lines.push(`profile.${key} cleared`);
    } else if (prev.profile[key] == null || prev.profile[key] === "") {
      lines.push(`profile.${key} set`);
    } else {
      lines.push(`profile.${key} changed`);
    }
  }

  const collections: { name: string; key: string; a: Record<string, unknown>[]; b: Record<string, unknown>[] }[] = [
    { name: "people", key: "person_id", a: prev.people, b: next.people },
    { name: "priorities", key: "priority_id", a: prev.priorities, b: next.priorities },
    { name: "usuals", key: "usual_id", a: prev.usuals, b: next.usuals },
    { name: "reminders", key: "reminder_id", a: prev.reminders, b: next.reminders },
    { name: "contacts", key: "contact_id", a: prev.contacts, b: next.contacts },
    { name: "agenda", key: "event_id", a: prev.agenda.level_recent, b: next.agenda.level_recent },
    { name: "chat", key: "turn_id", a: prev.chat_turns, b: next.chat_turns },
    { name: "negatives", key: "negative_id", a: prev.negatives, b: next.negatives },
  ];
  for (const col of collections) {
    const before = ids(col.a, col.key);
    const after = ids(col.b, col.key);
    const windowed = col.name === "agenda" || col.name === "chat" || col.name === "negatives";
    for (const [id, item] of after) {
      if (!before.has(id)) lines.push(`${col.name} +${labelOf(item, id)}`);
    }
    if (!windowed) {
      for (const [id, item] of before) {
        if (!after.has(id)) lines.push(`${col.name} −${labelOf(item, id)}`);
      }
    }
    for (const [id, item] of after) {
      const old = before.get(id);
      if (old && stable(old) !== stable(item)) {
        lines.push(`${col.name} ~${labelOf(item, id)}`);
      }
    }
  }
  if (prev.agenda.total !== next.agenda.total) {
    const delta = next.agenda.total - prev.agenda.total;
    lines.push(`agenda count ${delta > 0 ? "+" : ""}${delta} (now ${next.agenda.total})`);
  }
  return lines;
}

function clock(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
  } catch {
    return "";
  }
}

function resolvedMissingLabel(raw: unknown): string {
  if (!raw || typeof raw !== "object") return "—";
  const o = raw as Record<string, unknown>;
  const ids = Array.isArray(o.group_ids) ? o.group_ids : [];
  if (!ids.length) return "—";
  const week = typeof o.week_start === "string" ? o.week_start : "";
  return week ? `${ids.length} · ${week}` : String(ids.length);
}

function emailPendingLabel(profile: Record<string, unknown>): string {
  const draft = profile.pending_email_draft;
  if (draft && typeof draft === "object") {
    const d = draft as Record<string, unknown>;
    const who = typeof d.contact_name === "string" ? d.contact_name : "draft";
    const subject = typeof d.subject === "string" ? d.subject : "";
    return subject ? `${who}: ${subject}` : who;
  }
  const pick = profile.pending_email_pick;
  if (pick && typeof pick === "object") {
    const p = pick as Record<string, unknown>;
    const n = Array.isArray(p.candidates) ? p.candidates.length : 0;
    return n ? `pick among ${n}` : "pick";
  }
  return "—";
}

function pendingLabel(raw: unknown): string | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  const title = typeof o.title === "string" ? o.title : null;
  const action = typeof o.action === "string" ? o.action : null;
  const n = Array.isArray(o.slots) ? o.slots.length : 0;
  if (action && title) return `${action}: ${title}`;
  if (title && n) return `${title} · ${n} slots`;
  if (title) return title;
  return JSON.stringify(raw).slice(0, 48);
}

function clampWidth(px: number): number {
  if (!Number.isFinite(px)) return DEFAULT_WIDTH_PX;
  return Math.min(MAX_WIDTH_PX, Math.max(MIN_WIDTH_PX, Math.round(px)));
}

export default function DataInspector() {
  const [open, setOpen] = useState(false);
  const [width, setWidth] = useState<number>(DEFAULT_WIDTH_PX);
  const [resizing, setResizing] = useState(false);
  const [snap, setSnap] = useState<StoreSnap | null>(null);
  const [log, setLog] = useState<Mutation[]>([]);
  const [flash, setFlash] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const prev = useRef<StoreSnap | null>(null);
  const asideRef = useRef<HTMLElement | null>(null);
  const widthRef = useRef<number>(DEFAULT_WIDTH_PX);

  useEffect(() => {
    try {
      setOpen(localStorage.getItem(OPEN_KEY) === "1");
      const raw = localStorage.getItem(WIDTH_KEY);
      if (raw) {
        const parsed = Number(raw);
        if (Number.isFinite(parsed)) {
          const w = clampWidth(parsed);
          widthRef.current = w;
          setWidth(w);
        }
      }
    } catch {
      /* ignore */
    }
  }, []);

  const toggle = () => {
    setOpen((v) => {
      const next = !v;
      try {
        localStorage.setItem(OPEN_KEY, next ? "1" : "0");
      } catch {
        /* ignore */
      }
      return next;
    });
  };

  // Pointer-driven resize. On mobile the aside is a full-height modal
  // (see the max-width: 767px block in the CSS module) so resizing
  // would fight the slide-in gesture - skip below the breakpoint.
  const startResize = (e: React.PointerEvent<HTMLDivElement>) => {
    if (typeof window !== "undefined" && window.matchMedia("(max-width: 767px)").matches) {
      return;
    }
    e.preventDefault();
    setResizing(true);
    const target = e.currentTarget;
    try {
      target.setPointerCapture(e.pointerId);
    } catch {
      /* older browsers; still works via window listeners below */
    }
    // We measure from the aside's LEFT edge (fixed at x=0 for this
    // layout) so newWidth = pointerX - asideLeft. This survives
    // horizontal page scroll and browser zoom changes.
    const asideLeft = asideRef.current?.getBoundingClientRect().left ?? 0;
    const onMove = (evt: PointerEvent) => {
      const next = clampWidth(evt.clientX - asideLeft);
      widthRef.current = next;
      setWidth(next);
    };
    const onUp = () => {
      setResizing(false);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      try {
        target.releasePointerCapture(e.pointerId);
      } catch {
        /* ignore */
      }
      try {
        localStorage.setItem(WIDTH_KEY, String(widthRef.current));
      } catch {
        /* ignore */
      }
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  };

  // Keyboard accessibility: focus the resizer and adjust with arrows.
  // Shift+Arrow jumps by a larger step; Home/End snap to bounds.
  const nudgeResize = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 32 : 12;
    let next: number | null = null;
    if (e.key === "ArrowLeft") {
      next = clampWidth(widthRef.current - step);
    } else if (e.key === "ArrowRight") {
      next = clampWidth(widthRef.current + step);
    } else if (e.key === "Home") {
      next = MIN_WIDTH_PX;
    } else if (e.key === "End") {
      next = MAX_WIDTH_PX;
    }
    if (next === null) return;
    e.preventDefault();
    widthRef.current = next;
    setWidth(next);
    try {
      localStorage.setItem(WIDTH_KEY, String(next));
    } catch {
      /* ignore */
    }
  };

  const load = useCallback(async () => {
    try {
      const next = await api.get<StoreSnap>("/v1/admin/store");
      const before = prev.current;
      if (before) {
        const lines = diffSnaps(before, next);
        if (lines.length) {
          const at = clock(next.fetched_at);
          setLog((old) => [...lines.map((text) => ({ at, text })), ...old].slice(0, 24));
          setFlash(true);
          window.setTimeout(() => setFlash(false), 1600);
        }
      }
      prev.current = next;
      setSnap(next);
      setEnabled(true);
    } catch {
      setEnabled(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const id = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(id);
  }, [load]);

  if (!enabled) return null;

  const asideClass = [
    styles.rail,
    open ? styles.open : "",
    resizing ? styles.resizing : "",
  ]
    .filter(Boolean)
    .join(" ");
  const asideStyle = open ? { width: `${width}px` } : undefined;

  return (
    <>
      {open ? (
        <button type="button" className={styles.scrim} onClick={toggle} aria-label="Close data inspector" />
      ) : null}
      <aside
        ref={asideRef}
        className={asideClass}
        style={asideStyle}
        aria-label="Live store inspector"
      >
      <button
        type="button"
        className={styles.tab}
        onClick={toggle}
        aria-expanded={open}
        title={open ? "Collapse data inspector" : "Expand data inspector"}
      >
        <span className={flash ? `${styles.dot} ${styles.dotFlash}` : styles.dot} aria-hidden="true" />
        <span className={styles.tabLabel}>Data</span>
        <span className={open ? `${styles.chevron} ${styles.chevronOpen}` : styles.chevron} aria-hidden="true">
          <svg viewBox="0 0 16 16" width="14" height="14">
            <path
              d="M6 3.5 11 8 6 12.5"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.75"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </span>
      </button>
      {open && snap && (
        <div className={styles.panel}>
          <header className={styles.head}>
            <div>
              <h2>Live store</h2>
              <p>
                {snap.user_id} · polls every 2s
              </p>
            </div>
          </header>

          {log.length > 0 && (
            <section className={styles.section}>
              <h3>Mutations</h3>
              <ol className={styles.log}>
                {log.slice(0, 10).map((m, i) => (
                  <li key={`${m.at}-${m.text}-${i}`} className={i === 0 && flash ? styles.fresh : undefined}>
                    <span className={styles.when}>{m.at}</span>
                    <span>{m.text}</span>
                  </li>
                ))}
              </ol>
            </section>
          )}

          <Section title="Profile" changed={flash}>
            <Row k="email" v={String(snap.profile.email ?? "—")} />
            <Row k="tz" v={String(snap.profile.tz ?? "—")} />
            <Row k="dismissed week" v={String(snap.profile.dismissed_missing_week ?? "—")} />
            <Row k="resolved missing" v={resolvedMissingLabel(snap.profile.resolved_missing_week)} />
            <Row k="pending book" v={pendingLabel(snap.profile.pending_booking) ?? "—"} />
            <Row k="pending find" v={pendingLabel(snap.profile.pending_find) ?? "—"} />
            <Row k="pending email" v={emailPendingLabel(snap.profile)} />
          </Section>

          <Section title={`Priorities (${snap.priorities.length})`}>
            {snap.priorities.slice(0, 8).map((p) => (
              <Row key={String(p.priority_id)} k={String(p.weight ?? "")} v={String(p.text ?? "")} />
            ))}
            {snap.priorities.length === 0 && <p className={styles.empty}>None yet.</p>}
          </Section>

          <Section title={`Agenda · Level writes (${snap.agenda.level}/${snap.agenda.total})`}>
            {snap.agenda.level_recent.length === 0 && <p className={styles.empty}>No Level-created events.</p>}
            {snap.agenda.level_recent.map((e) => (
              <Row
                key={String(e.event_id)}
                k={clock(String(e.start ?? ""))}
                v={String(e.summary ?? "")}
              />
            ))}
          </Section>

          <Section title={`People (${snap.people.length})`}>
            {snap.people.slice(0, 8).map((p) => (
              <Row
                key={String(p.person_id)}
                k={String(p.relation ?? "")}
                v={String(p.display_name ?? "")}
              />
            ))}
            {snap.people.length === 0 && <p className={styles.empty}>None yet.</p>}
          </Section>

          <Section title={`Usuals (${snap.usuals.length})`}>
            {snap.usuals.slice(0, 6).map((u) => (
              <Row
                key={String(u.usual_id)}
                k={String(u.status ?? "")}
                v={String(u.display_summary ?? "")}
              />
            ))}
            {snap.usuals.length === 0 && <p className={styles.empty}>None yet.</p>}
          </Section>

          <Section title={`Reminders (${snap.reminders.length})`}>
            {snap.reminders.slice(0, 6).map((r) => (
              <Row key={String(r.reminder_id)} k={String(r.status ?? "")} v={String(r.text ?? "")} />
            ))}
            {snap.reminders.length === 0 && <p className={styles.empty}>None yet.</p>}
          </Section>

          <Section title={`Contacts (${snap.contacts.length})`}>
            {snap.contacts.slice(0, 6).map((c) => (
              <Row key={String(c.contact_id)} k={String(c.kind ?? "")} v={String(c.name ?? "")} />
            ))}
            {snap.contacts.length === 0 && <p className={styles.empty}>None yet.</p>}
          </Section>

          <Section title={`Chat (${snap.chat_turns.length} recent)`}>
            {snap.chat_turns.slice(0, 5).map((t) => (
              <Row key={String(t.turn_id)} k={String(t.role ?? "")} v={String(t.text ?? "")} />
            ))}
          </Section>

          {(() => {
            // profile["memory_bank"]["memories"] is written by
            // feedback → keep; render the tail here so the demo can
            // point at the row appearing after the user thumbs up.
            const raw = (snap.profile.memory_bank as Record<string, unknown> | undefined)
              ?.memories;
            const memories = Array.isArray(raw)
              ? (raw as Record<string, unknown>[])
              : [];
            return (
              <Section title={`Memory bank (${memories.length})`}>
                {memories.slice(-5).reverse().map((m, i) => (
                  <Row
                    key={String(m.id ?? i)}
                    k={String((Array.isArray(m.tags) ? m.tags[0] : "keep") ?? "keep")}
                    v={String(m.text ?? "")}
                  />
                ))}
                {memories.length === 0 && (
                  <p className={styles.empty}>None yet — thumbs-up an AI reply.</p>
                )}
              </Section>
            );
          })()}

          <Section title={`Negatives (${snap.negatives.length})`}>
            {snap.negatives.slice(0, 6).map((n) => (
              <Row
                key={String(n.negative_id)}
                k={String(n.agent ?? "")}
                v={String(n.value ?? n.reason ?? "")}
              />
            ))}
            {snap.negatives.length === 0 && (
              <p className={styles.empty}>None yet — thumbs-down an AI reply.</p>
            )}
          </Section>
        </div>
      )}
      {open ? (
        <div
          className={styles.resizer}
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize data inspector"
          aria-valuemin={MIN_WIDTH_PX}
          aria-valuemax={MAX_WIDTH_PX}
          aria-valuenow={width}
          tabIndex={0}
          onPointerDown={startResize}
          onKeyDown={nudgeResize}
        >
          <span className={styles.resizerGrip} aria-hidden="true" />
        </div>
      ) : null}
    </aside>
    </>
  );
}

function Section({
  title,
  children,
  changed,
}: {
  title: string;
  children: React.ReactNode;
  changed?: boolean;
}) {
  return (
    <section className={changed ? `${styles.section} ${styles.changed}` : styles.section}>
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className={styles.row}>
      <span className={styles.k}>{k}</span>
      <span className={styles.v}>{v}</span>
    </div>
  );
}
