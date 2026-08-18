"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Chat from "@/components/Chat";
import { api } from "@/lib/api";
import type { CarePerson, Priority, Usual } from "@/lib/types";
import styles from "./profile.module.css";

type ProfileResp = { people: CarePerson[]; usuals: Usual[]; priorities: Priority[] };

const WEEKDAY_LABEL = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const BAND_LABEL: Record<string, string> = {
  early_morning: "5\u20139am",
  morning: "9am\u201312pm",
  midday: "12\u20132pm",
  afternoon: "2\u20135pm",
  evening: "5\u20138pm",
  night: "8pm\u2013late",
  overnight: "overnight",
};

const RELATION_LABEL: Record<string, string> = {
  self: "you",
  child: "child",
  elder: "elder",
  partner: "partner",
  friend: "friend",
  other: "other",
};

type Role = {
  key: string;
  label: string;
  people: CarePerson[];
};

function groupByRole(people: CarePerson[]): Role[] {
  const buckets = new Map<string, CarePerson[]>();
  for (const person of people) {
    const key = person.is_self ? "self" : person.relation || "other";
    const arr = buckets.get(key) ?? [];
    arr.push(person);
    buckets.set(key, arr);
  }
  const order = ["self", "child", "elder", "partner", "friend", "other"];
  return order
    .filter((k) => buckets.has(k))
    .map((k) => ({
      key: k,
      label:
        k === "self"
          ? "You"
          : k === "child"
            ? "Children"
            : k === "elder"
              ? "Elders"
              : k === "partner"
                ? "Partner"
                : k === "friend"
                  ? "Friends"
                  : "Others",
      people: (buckets.get(k) ?? []).sort((a, b) => a.display_name.localeCompare(b.display_name)),
    }));
}

function buildSummary(people: CarePerson[], usuals: Usual[], priorities: Priority[]): string {
  const kept = people.filter((p) => p.status === "kept" || p.status === "proposed");
  const kids = kept.filter((p) => !p.is_self && p.relation === "child");
  const elders = kept.filter((p) => !p.is_self && p.relation === "elder");
  const partner = kept.find((p) => !p.is_self && p.relation === "partner");

  const parts: string[] = [];
  if (kids.length > 0) {
    parts.push(
      `child care for ${kids.map((k) => k.display_name).join(" and ")}`,
    );
  }
  if (elders.length > 0) {
    parts.push(
      `elder support for ${elders.map((e) => e.display_name).join(" and ")}`,
    );
  }
  if (partner) parts.push(`co-parenting with ${partner.display_name}`);

  const rhythm = usuals.filter((u) => u.status === "kept" || u.status === "proposed").length;
  if (rhythm > 0) {
    parts.push(`${rhythm} weekly usual${rhythm === 1 ? "" : "s"}`);
  }
  const prio = priorities.filter((p) => p.status === "kept").length;
  if (prio > 0) {
    parts.push(`${prio} priorit${prio === 1 ? "y" : "ies"} you asked me to weigh`);
  }
  if (parts.length === 0) {
    return "I don\u2019t have a picture of your care load yet \u2014 re-read your calendar and tell me anything else Level should know.";
  }
  return `Right now Level thinks you hold ${parts.join(", ")}.`;
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

  async function keepAll(entity: "person" | "usual") {
    if (!data || busy) return;
    setBusy(true);
    try {
      const items =
        entity === "person"
          ? data.people.filter((p) => p.status === "proposed")
          : data.usuals.filter((u) => u.status === "proposed");
      for (const it of items) {
        const id = entity === "person" ? (it as CarePerson).person_id : (it as Usual).usual_id;
        await api.post("/v1/profile/keep_not_me", { entity, id, status: "kept" });
      }
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function setStatusFor(entity: "person" | "usual" | "priority", id: string, status: "kept" | "not_me") {
    setBusy(true);
    try {
      await api.post("/v1/profile/keep_not_me", { entity, id, status });
      await load();
    } finally {
      setBusy(false);
    }
  }

  const people = data?.people ?? [];
  const usuals = data?.usuals ?? [];
  const priorities = data?.priorities ?? [];
  const summary = useMemo(() => buildSummary(people, usuals, priorities), [people, usuals, priorities]);

  const roles = groupByRole(people.filter((p) => p.status !== "not_me"));
  const activeUsuals = usuals.filter((u) => u.status !== "not_me");
  const activePriorities = priorities.filter((p) => p.status === "kept");
  const pendingPeople = people.filter((p) => p.status === "proposed").length;
  const pendingUsuals = usuals.filter((u) => u.status === "proposed").length;

  return (
    <div className={styles.wrap}>
      <header className={styles.header}>
        <h1>About me</h1>
        <p className={styles.sub}>
          What Level has gathered about your care load. Keep what fits, dismiss what
          doesn&apos;t &mdash; and tell Level more below.
        </p>
        <div className={styles.headerActions}>
          <button className="button-ghost" onClick={refresh} disabled={busy}>
            {busy ? "Re-reading calendar\u2026" : "Re-read calendar"}
          </button>
          {status ? <span className={styles.statusChip}>{status}</span> : null}
        </div>
      </header>

      <section className={`card ${styles.summary}`}>
        <p className={styles.summaryLead}>{summary}</p>
        <ul className={styles.metrics}>
          <li>
            <span className={styles.metricValue}>{roles.reduce((n, r) => n + r.people.length, 0)}</span>
            <span className={styles.metricLabel}>people</span>
          </li>
          <li>
            <span className={styles.metricValue}>{activeUsuals.length}</span>
            <span className={styles.metricLabel}>usuals</span>
          </li>
          <li>
            <span className={styles.metricValue}>{activePriorities.length}</span>
            <span className={styles.metricLabel}>priorities</span>
          </li>
        </ul>
      </section>

      <div className={styles.grid}>
        <div className={styles.mainCol}>
          <section className={styles.section}>
            <div className={styles.sectionHead}>
              <h2>Care roles</h2>
              {pendingPeople > 0 ? (
                <button
                  type="button"
                  className={styles.linkAction}
                  onClick={() => void keepAll("person")}
                  disabled={busy}
                >
                  Keep all {pendingPeople}
                </button>
              ) : null}
            </div>
            {roles.length === 0 ? (
              <p className={styles.meta}>
                Nothing yet. Tap &ldquo;Re-read calendar&rdquo; and Level will draft the
                people it thinks you hold.
              </p>
            ) : (
              <div className={styles.rolesStack}>
                {roles.map((role) => (
                  <div key={role.key} className={styles.roleGroup}>
                    <h3>{role.label}</h3>
                    <ul className={styles.pillList}>
                      {role.people.map((p) => (
                        <li
                          key={p.person_id}
                          className={`${styles.personPill} ${p.status === "proposed" ? styles.pending : ""}`}
                        >
                          <span className={styles.pillName}>{p.display_name}</span>
                          <span className={styles.pillRelation}>
                            {RELATION_LABEL[p.relation] ?? p.relation}
                          </span>
                          <div className={styles.pillActions}>
                            {p.status === "proposed" ? (
                              <button
                                type="button"
                                className={styles.keepBtn}
                                onClick={() => void setStatusFor("person", p.person_id, "kept")}
                                disabled={busy}
                              >
                                Keep
                              </button>
                            ) : null}
                            <button
                              type="button"
                              className={styles.notMeBtn}
                              onClick={() => void setStatusFor("person", p.person_id, "not_me")}
                              disabled={busy}
                              title="Not me / not this person"
                            >
                              Not me
                            </button>
                          </div>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className={styles.section}>
            <div className={styles.sectionHead}>
              <h2>Weekly usuals</h2>
              {pendingUsuals > 0 ? (
                <button
                  type="button"
                  className={styles.linkAction}
                  onClick={() => void keepAll("usual")}
                  disabled={busy}
                >
                  Keep all {pendingUsuals}
                </button>
              ) : null}
            </div>
            {activeUsuals.length === 0 ? (
              <p className={styles.meta}>
                No repeating patterns yet. A few weeks of calendar history helps Level spot the rhythm.
              </p>
            ) : (
              <ul className={styles.usualList}>
                {activeUsuals.map((u) => (
                  <li key={u.usual_id} className={`${styles.usualRow} ${u.status === "proposed" ? styles.pending : ""}`}>
                    <div className={styles.usualBody}>
                      <p className={styles.usualTitle}>{u.display_summary}</p>
                      <p className={styles.usualMeta}>
                        {WEEKDAY_LABEL[u.weekday] ?? "?"} &middot; {BAND_LABEL[u.hour_band] ?? u.hour_band}
                      </p>
                    </div>
                    <div className={styles.usualActions}>
                      {u.status === "proposed" ? (
                        <button
                          type="button"
                          className={styles.keepBtn}
                          onClick={() => void setStatusFor("usual", u.usual_id, "kept")}
                          disabled={busy}
                        >
                          Keep
                        </button>
                      ) : null}
                      <button
                        type="button"
                        className={styles.notMeBtn}
                        onClick={() => void setStatusFor("usual", u.usual_id, "not_me")}
                        disabled={busy}
                      >
                        Not me
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {activePriorities.length > 0 && (
            <section className={styles.section}>
              <div className={styles.sectionHead}>
                <h2>Priorities</h2>
              </div>
              <ul className={styles.priorityList}>
                {activePriorities.map((p) => (
                  <li key={p.priority_id} className={styles.priorityRow}>
                    <span className={styles.weightPill}>weight {p.weight}</span>
                    <p>{p.text}</p>
                    <button
                      type="button"
                      className={styles.notMeBtn}
                      onClick={() => void setStatusFor("priority", p.priority_id, "not_me")}
                      disabled={busy}
                    >
                      Not me
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>

        <aside className={styles.rail} aria-label="Correct Level">
          <Chat
            title="Correct Level"
            lead={
              "Tell me a priority (\u201cnever miss elder therapy\u201d), rename a person, or say something I misread \u2014 I&rsquo;ll update the profile."
            }
            placeholder="&ldquo;Sam is my nephew, not my child.&rdquo;"
            onAfterReply={() => void load()}
          />
        </aside>
      </div>
    </div>
  );
}
