"use client";

import { useCallback, useEffect, useState } from "react";
import Chat from "@/components/Chat";
import EventCard from "@/components/EventCard";
import RemindersPanel from "@/components/RemindersPanel";
import RoleLoadBar from "@/components/RoleLoadBar";
import { api, ApiError } from "@/lib/api";
import type { TodayResponse, WhoAmI } from "@/lib/types";
import styles from "./today.module.css";

export default function TodayPage() {
  const [who, setWho] = useState<WhoAmI | null>(null);
  const [data, setData] = useState<TodayResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [needsConnect, setNeedsConnect] = useState(false);
  const [remindersTick, setRemindersTick] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
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
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setNeedsConnect(true);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading) {
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

  return (
    <div className={styles.wrap}>
      <div className={styles.titleRow}>
        <div>
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
        </div>
      </div>

      {data?.missing_usuals && data.missing_usuals.length > 0 && (
        <section className={styles.banner}>
          <strong>Missing today</strong>
          <ul>
            {data.missing_usuals.map((m) => (
              <li key={m.usual_id}>
                <span>{m.display_summary}</span>
                <div className={styles.bannerActions}>
                  <button className="button-ghost">Put it back</button>
                  <button className="button-ghost">This week is different</button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className={styles.workspace}>
        <div className={styles.mainCol}>
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

        <aside className={styles.rail} aria-label="Reminders and ask Level">
          <section className={styles.railBlock}>
            <RemindersPanel key={remindersTick} onChange={load} />
          </section>
          <section className={styles.railBlock}>
            <Chat
              lead="Ask about your day, book a time, or draft an email &mdash; and Level will draft here."
              placeholder='"What&rsquo;s crowding this week?" or "email Alpha&rsquo;s teacher, sick today"'
              onAfterReply={() => {
                setRemindersTick((n) => n + 1);
                void load();
              }}
            />
          </section>
        </aside>
      </div>
    </div>
  );
}
