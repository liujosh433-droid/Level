import type { CSSProperties } from "react";
import type { TodayEvent } from "@/lib/types";
import { activityColor, activityEmoji, activityLabel } from "@/lib/activityIcons";
import styles from "./EventCard.module.css";

function formatRange(startIso: string, endIso: string): string {
  const start = new Date(startIso);
  const end = new Date(endIso);
  const opts: Intl.DateTimeFormatOptions = { hour: "numeric", minute: "2-digit" };
  return `${start.toLocaleTimeString([], opts)} - ${end.toLocaleTimeString([], opts)}`;
}

export default function EventCard({ event, dense = false }: { event: TodayEvent; dense?: boolean }) {
  const color = activityColor(event.activity_type);
  const style = { ["--event-color" as string]: color } as CSSProperties;
  return (
    <article className={dense ? styles.dense : styles.card} style={style}>
      <div className={styles.eventArt} aria-hidden>
        <span className={styles.emoji}>{activityEmoji(event.activity_type)}</span>
      </div>
      <div className={styles.eventBody}>
        <div className={styles.eventMeta}>
          <span className={styles.when}>{formatRange(event.start, event.end)}</span>
          <span className={styles.kindChip}>{activityLabel(event.activity_type)}</span>
        </div>
        <span className={styles.eventTitle}>{event.summary}</span>
        {event.origin === "level" && (
          <span className="pill pill--signal" title={event.level_reason ?? ""}>
            Booked by Level
          </span>
        )}
        {event.people.length > 0 && (
          <div className={styles.tags}>
            {event.people.map((p) => (
              <span key={p.person_id} className="pill">
                {p.display_name}
              </span>
            ))}
          </div>
        )}
        {event.reminders.length > 0 && (
          <ul className={styles.eventCues}>
            {event.reminders.map((r) => (
              <li key={r.reminder_id}>{r.text}</li>
            ))}
          </ul>
        )}
      </div>
    </article>
  );
}
