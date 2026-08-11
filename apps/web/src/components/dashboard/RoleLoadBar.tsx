"use client";

import type { WeekRoleLoad } from "@/lib/api";
import styles from "./RoleLoadBar.module.css";

export function RoleLoadBar({
  load,
}: {
  load: WeekRoleLoad[] | null | undefined;
}) {
  const rows = (load || []).filter((r) => r.percent > 0);
  if (rows.length === 0) return null;

  return (
    <div className={styles.wrap}>
      <p className={styles.kicker}>This week&rsquo;s held load</p>
      <div
        className={styles.bar}
        role="img"
        aria-label={rows
          .map((r) => `${r.label} ${r.percent} percent`)
          .join(", ")}
      >
        {rows.map((r) => (
          <span
            key={r.role_id}
            className={styles.seg}
            style={{
              width: `${Math.max(r.percent, 2)}%`,
              background: r.color,
            }}
            title={`${r.label}: ${r.percent}%`}
          />
        ))}
      </div>
      <ul className={styles.legend}>
        {rows.map((r) => (
          <li key={r.role_id}>
            <span className={styles.dot} style={{ background: r.color }} />
            <span className={styles.label}>{r.label}</span>
            <span className={styles.pct}>{r.percent}%</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
