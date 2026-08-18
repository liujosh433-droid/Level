"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Chat from "@/components/Chat";
import EventCard from "@/components/EventCard";
import RemindersPanel from "@/components/RemindersPanel";
import RoleLoadBar from "@/components/RoleLoadBar";
import { api, ApiError } from "@/lib/api";
import { buildPersonColorMap } from "@/lib/personColor";
import type { MissingUsualWeek, TodayResponse, WhoAmI } from "@/lib/types";
import styles from "./today.module.css";

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

function groupMissingByWeekday(items: MissingUsualWeek[]): { weekday: number; items: MissingUsualWeek[] }[] {
  const buckets = new Map<number, MissingUsualWeek[]>();
  for (const m of items) {
    const arr = buckets.get(m.weekday) ?? [];
    arr.push(m);
    buckets.set(m.weekday, arr);
  }
  return Array.from(buckets.entries())
    .sort(([a], [b]) => a - b)
    .map(([weekday, arr]) => ({
      weekday,
      items: arr.sort((a, b) => (a.typical_start ?? "").localeCompare(b.typical_start ?? "")),
    }));
}

export default function TodayPage() {
  const [who, setWho] = useState<WhoAmI | null>(null);
  const [data, setData] = useState<TodayResponse | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [needsConnect, setNeedsConnect] = useState(false);
  const [remindersTick, setRemindersTick] = useState(0);
  const [dismissMissing, setDismissMissing] = useState(false);

  const load = useCallback(async () => {
    try {
      const me = await api.get<WhoAmI>("/v1/me");
      setWho(me);
      if (!me.google_connected) {
        setNeedsConnect(true);
        setData(null);
        return;
      }
      const today = await api.get<TodayResponse>("/v1/today");
      setData(today);
      setNeedsConnect(false);
      setDismissMissing(Boolean(today.missing_usuals_week_dismissed));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setNeedsConnect(true);
      }
    }
  }, []);

  useEffect(() => {
    void load().finally(() => setInitialLoading(false));
  }, [load]);

  const missingByDay = useMemo(
    () => groupMissingByWeekday(data?.missing_usuals_week ?? []),
    [data?.missing_usuals_week],
  );

  const colorMap = useMemo(
    () => buildPersonColorMap((data?.missing_usuals_week ?? []).map((m) => m.person_id)),
    [data?.missing_usuals_week],
  );

  if (initialLoading && !data) {
    return <p className={styles.meta}>Loading today&hellip;</p>;
  }
  if (needsConnect) {
    return (
      <section className={styles.empty}>
        <h1>Connect Google to get started</h1>
        <p className={styles.meta}>
          Level reads your calendar so it can spot what&apos;s usual and what&apos;s missing.
        </p>
        <a className="button-primary" href="/v1/auth/google/start">
          Connect Google
        </a>
      </section>
    );
  }

  const dateLabel = data
    ? new Date(data.date).toLocaleDateString([], {
        weekday: "long",
        month: "short",
        day: "numeric",
      })
    : null;
  const greetingName = who?.email?.split("@")[0] ?? "there";
  const weekday = data
    ? new Date(data.date).toLocaleDateString([], { weekday: "long" })
    : null;

  const hasMissing = !dismissMissing && missingByDay.length > 0;

  async function dismissMissingWeek() {
    setDismissMissing(true);
    try {
      await api.post("/v1/today/missing-week/dismiss", {});
    } catch {
      setDismissMissing(false);
    }
  }

  return (
    <div className={styles.wrap}>
      <div className={styles.workspace}>
        <div className={styles.mainCol}>
          <header className={styles.hero}>
            {dateLabel && <p className={styles.dateLabel}>{dateLabel}</p>}
            <h1>
              Hi {greetingName}
              {weekday ? `, happy ${weekday}` : ""}!
            </h1>
            <p className={styles.sub}>
              {data?.today.length
                ? `${data.today.length} on your calendar today.`
                : "Your calendar is clear today."}
            </p>
            <RoleLoadBar load={data?.week_load} />
          </header>

          {hasMissing && (
            <section className={`${styles.block} ${styles.missing}`}>
              <div className={styles.missingHead}>
                <div>
                  <h2>Usuals missing this week</h2>
                  <p className={styles.missingHint}>
                    Accident? Tell Level in the chat and it&rsquo;ll put them back. Otherwise, dismiss the whole set.
                  </p>
                </div>
                <button
                  className="button-ghost"
                  onClick={() => void dismissMissingWeek()}
                  title="Hide missing usuals until next week"
                >
                  This week is different
                </button>
              </div>
              <ul className={styles.missingWeek}>
                {missingByDay.map((day) => (
                  <li key={day.weekday} className={styles.missingDay}>
                    <span className={styles.missingDayLabel}>{WEEKDAY_LABEL[day.weekday]}</span>
                    <ul className={styles.missingList}>
                      {day.items.map((m) => {
                        const c = colorMap.get(m.person_id);
                        const time =
                          m.typical_start && m.typical_end
                            ? `${m.typical_start}\u2013${m.typical_end}`
                            : "—";
                        return (
                          <li key={m.group_id} className={styles.missingRow}>
                            <span className={styles.missingTime}>{time}</span>
                            {m.person_name ? (
                              <span
                                className={styles.personChip}
                                style={c ? { background: c.bg, borderColor: c.border, color: c.ink } : undefined}
                              >
                                {m.person_name}
                                {m.person_relation ? (
                                  <span className={styles.chipRel}>
                                    {" "}
                                    {RELATION_LABEL[m.person_relation] ?? m.person_relation}
                                  </span>
                                ) : null}
                              </span>
                            ) : null}
                            <span className={styles.missingCategory}>{m.category_label}</span>
                          </li>
                        );
                      })}
                    </ul>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section className={styles.block}>
            <h2>Today</h2>
            {data?.today.length ? (
              <ul className={styles.list}>
                {data.today.map((e) => (
                  <li key={e.event_id}>
                    <EventCard event={e} />
                  </li>
                ))}
              </ul>
            ) : (
              <p className={styles.meta}>Nothing on the calendar.</p>
            )}
          </section>

          <section className={`${styles.block} ${styles.tomorrow}`}>
            <h2>Tomorrow</h2>
            {data?.tomorrow.length ? (
              <ul className={styles.list}>
                {data.tomorrow.map((e) => (
                  <li key={e.event_id}>
                    <EventCard event={e} />
                  </li>
                ))}
              </ul>
            ) : (
              <p className={styles.meta}>Nothing on the calendar yet.</p>
            )}
          </section>
        </div>

        <aside className={styles.rail} aria-label="Ask Level and reminders">
          <section className={styles.railBlock}>
            <Chat
              lead="Ask about your day, book a time, or draft an email &mdash; and Level will draft here."
              placeholder='"What&rsquo;s crowding this week?" or "put back Tuesday Nova pickup"'
              busyHints={[
                "Looking at your calendar\u2026",
                "Weighing what this would crowd out\u2026",
                "Checking your care load\u2026",
                "Almost there\u2026",
              ]}
              onAfterReply={() => {
                setRemindersTick((n) => n + 1);
                void load();
              }}
            />
          </section>
          <section className={styles.railBlock}>
            <RemindersPanel key={remindersTick} onChange={load} />
          </section>
        </aside>
      </div>
    </div>
  );
}
