"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Reminder } from "@/lib/types";
import { activityEmoji, activityLabel } from "@/lib/activityIcons";
import styles from "./RemindersPanel.module.css";

type RemindersResp = { reminders: Reminder[] };

export default function RemindersPanel({ onChange }: { onChange?: () => void }) {
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.get<RemindersResp>("/v1/reminders");
      setReminders(r.reminders);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function dismiss(id: string) {
    setBusyId(id);
    try {
      await api.post(`/v1/reminders/${id}/dismiss`, {});
      await load();
      onChange?.();
    } finally {
      setBusyId(null);
    }
  }

  const active = reminders.filter((r) => r.status === "active");

  return (
    <section className={styles.wrap} aria-label="Reminders">
      <header className={styles.header}>
        <h2>Reminders</h2>
        <span className={styles.count}>{active.length}</span>
      </header>
      {loading && <p className={styles.empty}>Loading...</p>}
      {!loading && active.length === 0 && (
        <p className={styles.empty}>
          None yet. Tell the assistant &ldquo;I keep forgetting the soccer shoes&rdquo; and it&apos;ll
          save one.
        </p>
      )}
      <ul className={styles.list}>
        {active.map((r) => (
          <li key={r.reminder_id} className={styles.item}>
            <span className={styles.emoji} aria-hidden="true">
              {activityEmoji(r.match?.activity_type)}
            </span>
            <div className={styles.body}>
              <div className={styles.text}>{r.text}</div>
              <div className={styles.meta}>{activityLabel(r.match?.activity_type)}</div>
            </div>
            <button
              className={styles.dismiss}
              onClick={() => dismiss(r.reminder_id)}
              disabled={busyId === r.reminder_id}
              aria-label={`Dismiss ${r.text}`}
              title="Remove this reminder"
            >
              ×
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
