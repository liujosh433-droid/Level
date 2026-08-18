"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Chat from "@/components/Chat";
import { api } from "@/lib/api";
import { buildPersonColorMap } from "@/lib/personColor";
import type { CarePerson, Priority, Usual } from "@/lib/types";
import styles from "./profile.module.css";

type UsualsMeta = {
  days_back: number;
  weeks_observed: number;
  events_scanned: number;
  min_repeats: number;
};

type ProfileResp = {
  people: CarePerson[];
  usuals: Usual[];
  priorities: Priority[];
  usuals_meta?: UsualsMeta;
};

const WEEKDAY_ORDER = [0, 1, 2, 3, 4, 5, 6];
const WEEKDAY_LABEL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

const RELATION_LABEL: Record<string, string> = {
  self: "You",
  child: "Kid",
  elder: "Elder",
  coparent: "Co-parent",
  partner: "Partner",
  friend: "Friend",
  other: "Other",
};

const RELATION_GROUP_LABEL: Record<string, string> = {
  child: "Kids",
  elder: "Elders",
  coparent: "Co-parent",
  partner: "Partner",
  friend: "Friends",
  other: "Others",
};

// "self" intentionally omitted — showing "You: <name>" isn't helpful.
const GROUP_ORDER = ["child", "elder", "coparent", "partner", "friend", "other"];

const BAND_FALLBACK: Record<string, string> = {
  early_morning: "5\u20139am",
  morning: "9am\u201312pm",
  midday: "12\u20132pm",
  afternoon: "2\u20135pm",
  evening: "5\u20138pm",
  night: "8pm\u2013late",
  overnight: "overnight",
};

function summarize(people: CarePerson[], usuals: Usual[], priorities: Priority[]): string {
  const kept = people.filter((p) => p.status !== "not_me");
  const kids = kept.filter((p) => !p.is_self && p.relation === "child");
  const elders = kept.filter((p) => !p.is_self && p.relation === "elder");
  const partner = kept.find((p) => !p.is_self && (p.relation === "partner" || p.relation === "coparent"));

  const parts: string[] = [];
  if (kids.length > 0) {
    parts.push(`child care for ${kids.map((k) => k.display_name).join(" and ")}`);
  }
  if (elders.length > 0) {
    parts.push(`elder support for ${elders.map((e) => e.display_name).join(" and ")}`);
  }
  if (partner) parts.push(`co-parenting with ${partner.display_name}`);

  const rhythm = usuals.filter((u) => u.status !== "not_me").length;
  if (rhythm > 0) parts.push(`${rhythm} weekly usual${rhythm === 1 ? "" : "s"}`);
  const prio = priorities.filter((p) => p.status !== "not_me").length;
  if (prio > 0) parts.push(`${prio} priorit${prio === 1 ? "y" : "ies"} you asked me to weigh`);

  if (parts.length === 0) {
    return "I don\u2019t have a picture of your care load yet \u2014 tap Re-read calendar or tell me anything I should know.";
  }
  return `Right now Level thinks you hold ${parts.join(", ")}.`;
}

function groupPeopleForRoles(people: CarePerson[]): { label: string; people: CarePerson[] }[] {
  const buckets = new Map<string, CarePerson[]>();
  for (const p of people) {
    if (p.status === "not_me" || p.is_self) continue;
    const key = p.relation || "other";
    const arr = buckets.get(key) ?? [];
    arr.push(p);
    buckets.set(key, arr);
  }
  return GROUP_ORDER.filter((k) => buckets.has(k)).map((k) => ({
    label: RELATION_GROUP_LABEL[k] ?? k,
    people: (buckets.get(k) ?? []).sort((a, b) => a.display_name.localeCompare(b.display_name)),
  }));
}

function bandRank(band: string): number {
  const order = ["early_morning", "morning", "midday", "afternoon", "evening", "night", "overnight"];
  const i = order.indexOf(band);
  return i === -1 ? 99 : i;
}

function minutesFromTypical(hm: string | null | undefined): number {
  if (!hm) return 24 * 60;
  const m = hm.match(/^(\d{1,2})(?::(\d{2}))?(am|pm)$/i);
  if (!m) return 24 * 60;
  let hour = Number(m[1]) % 12;
  const minute = Number(m[2] ?? 0);
  if (m[3].toLowerCase() === "pm") hour += 12;
  return hour * 60 + minute;
}

function formatTime(u: Usual): string {
  return u.typical_start && u.typical_end
    ? `${u.typical_start}\u2013${u.typical_end}`
    : BAND_FALLBACK[u.hour_band] ?? u.hour_band;
}

function groupUsualsByWeekday(usuals: Usual[]): { weekday: number; items: Usual[] }[] {
  const buckets = new Map<number, Usual[]>();
  for (const u of usuals) {
    if (u.status === "not_me") continue;
    const arr = buckets.get(u.weekday) ?? [];
    arr.push(u);
    buckets.set(u.weekday, arr);
  }
  return WEEKDAY_ORDER.filter((wd) => buckets.has(wd)).map((wd) => ({
    weekday: wd,
    items: (buckets.get(wd) ?? []).sort((a, b) => {
      const aMin = a.typical_start ? minutesFromTypical(a.typical_start) : bandRank(a.hour_band) * 240;
      const bMin = b.typical_start ? minutesFromTypical(b.typical_start) : bandRank(b.hour_band) * 240;
      return aMin - bMin;
    }),
  }));
}

export default function ProfilePage() {
  const [data, setData] = useState<ProfileResp | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  const load = useCallback(async () => {
    const p = await api.get<ProfileResp>("/v1/profile");
    setData(p);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function refresh() {
    if (busy) return;
    setBusy(true);
    setStatus(null);
    try {
      await api.post("/v1/profile/refresh", {});
      await load();
      setStatus("Re-read your calendar.");
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function removePriority(priorityId: string) {
    setData((prev) =>
      prev
        ? { ...prev, priorities: prev.priorities.filter((p) => p.priority_id !== priorityId) }
        : prev,
    );
    try {
      await api.del(`/v1/profile/priorities/${encodeURIComponent(priorityId)}`);
    } catch (err) {
      await load();
      setStatus(err instanceof Error ? err.message : "Couldn't remove that priority.");
    }
  }

  const people = data?.people ?? [];
  const usuals = data?.usuals ?? [];
  const priorities = data?.priorities ?? [];

  const summary = useMemo(() => summarize(people, usuals, priorities), [people, usuals, priorities]);
  const roles = useMemo(() => groupPeopleForRoles(people), [people]);
  const week = useMemo(() => groupUsualsByWeekday(usuals), [usuals]);
  const activePriorities = priorities.filter((p) => p.status !== "not_me");
  const colorMap = useMemo(
    () =>
      buildPersonColorMap(
        people.filter((p) => p.status !== "not_me").map((p) => p.person_id),
      ),
    [people],
  );

  return (
    <div className={styles.wrap}>
      <header className={styles.header}>
        <div className={styles.headerCopy}>
          <h1>About me</h1>
          <p className={styles.sub}>
            What Level has learned about your care load. To change anything, just tell me in the chat.
          </p>
        </div>
        <div className={styles.headerActions}>
          {status ? <span className={styles.statusChip}>{status}</span> : null}
          <button className="button-ghost" onClick={refresh} disabled={busy}>
            {busy ? "Re-reading calendar\u2026" : "Re-read calendar"}
          </button>
        </div>
      </header>

      <section className={`card ${styles.summary}`}>
        <p className={styles.summaryLead}>{summary}</p>
      </section>

      <div className={styles.grid}>
        <div className={styles.mainCol}>
          <section className={styles.section}>
            <h2>Care roles</h2>
            {roles.length === 0 ? (
              <p className={styles.meta}>
                Nothing yet. Tap Re-read calendar and Level will infer the people you hold.
              </p>
            ) : (
              <dl className={styles.roleList}>
                {roles.map((r) => (
                  <div key={r.label} className={styles.roleRow}>
                    <dt>{r.label}</dt>
                    <dd className={styles.rolePeople}>
                      {r.people.map((p) => {
                        const c = colorMap.get(p.person_id);
                        return (
                          <span
                            key={p.person_id}
                            className={styles.personChip}
                            style={c ? { background: c.bg, borderColor: c.border, color: c.ink } : undefined}
                          >
                            {p.display_name}
                          </span>
                        );
                      })}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
          </section>

          <section className={styles.section}>
            <h2>Weekly usuals</h2>
            {data?.usuals_meta ? (
              <p className={styles.inferNote}>
                Level scanned the last {data.usuals_meta.days_back} days of your calendar (
                {data.usuals_meta.events_scanned} past events across{" "}
                {data.usuals_meta.weeks_observed}{" "}
                {data.usuals_meta.weeks_observed === 1 ? "week" : "weeks"}), then grouped by person,
                weekday, and time-of-day. Anything that repeated at least {data.usuals_meta.min_repeats}{" "}
                times shows up here. Future events aren&rsquo;t counted &mdash; only what actually
                happened.
              </p>
            ) : null}
            {week.length === 0 ? (
              <p className={styles.meta}>
                No repeating patterns yet. A few weeks of calendar history helps Level spot the rhythm.
              </p>
            ) : (
              <ul className={styles.weekList}>
                {week.map((day) => (
                  <li key={day.weekday} className={styles.dayRow}>
                    <span className={styles.dayLabel}>{WEEKDAY_LABEL[day.weekday]}</span>
                    <ul className={styles.dayEvents}>
                      {day.items.map((u) => {
                        const c = colorMap.get(u.person_id);
                        return (
                          <li key={u.usual_id} className={styles.eventLine}>
                            <span className={styles.eventTime}>{formatTime(u)}</span>
                            {u.person_name ? (
                              <span
                                className={styles.personChip}
                                style={c ? { background: c.bg, borderColor: c.border, color: c.ink } : undefined}
                              >
                                {u.person_name}
                                {u.person_relation ? (
                                  <span className={styles.chipRel}>
                                    {" "}
                                    {RELATION_LABEL[u.person_relation] ?? u.person_relation}
                                  </span>
                                ) : null}
                              </span>
                            ) : null}
                            <span className={styles.eventSummary}>{u.display_summary}</span>
                          </li>
                        );
                      })}
                    </ul>
                  </li>
                ))}
              </ul>
            )}
          </section>

        </div>

        <aside className={styles.rail} aria-label="Correct Level">
          <Chat
            title="Correct Level"
            lead={
              "Tell me anything I got wrong \u2014 rename a person, add a priority (\u201cnever miss elder therapy\u201d), or drop a usual I invented."
            }
            placeholder="&ldquo;Sam is my nephew, not my child.&rdquo;"
            onAfterReply={() => void load()}
          />

          <section className={styles.railSection} aria-labelledby="priorities-heading">
            <h2 id="priorities-heading" className={styles.railHeading}>
              Priorities
            </h2>
            <p className={styles.railHint}>Things you&rsquo;ve asked me to weigh.</p>
            {activePriorities.length === 0 ? (
              <p className={styles.meta}>
                Nothing yet. Try &ldquo;never miss elder therapy&rdquo; or &ldquo;keep Friday evenings clear&rdquo;.
              </p>
            ) : (
              <ul className={styles.priorityList}>
                {activePriorities.map((p) => (
                  <li key={p.priority_id} className={styles.priorityRow}>
                    <svg
                      className={styles.priorityIcon}
                      viewBox="0 0 24 24"
                      width="14"
                      height="14"
                      aria-hidden="true"
                    >
                      <path
                        fill="currentColor"
                        d="M12 2l2.4 6.9 7.3.3-5.8 4.5 2 7-5.9-4.2-5.9 4.2 2-7L2.3 9.2l7.3-.3L12 2z"
                      />
                    </svg>
                    <span className={styles.priorityText}>{p.text}</span>
                    <button
                      type="button"
                      className={styles.priorityDelete}
                      onClick={() => void removePriority(p.priority_id)}
                      aria-label={`Remove priority: ${p.text}`}
                      title="Remove this priority"
                    >
                      <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
                        <path
                          fill="currentColor"
                          d="M18.3 5.7a1 1 0 0 0-1.4 0L12 10.6 7.1 5.7a1 1 0 0 0-1.4 1.4L10.6 12l-4.9 4.9a1 1 0 1 0 1.4 1.4L12 13.4l4.9 4.9a1 1 0 0 0 1.4-1.4L13.4 12l4.9-4.9a1 1 0 0 0 0-1.4z"
                        />
                      </svg>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}
