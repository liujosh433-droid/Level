import type { WeekLoadRow } from "@/lib/types";
import styles from "./RoleLoadBar.module.css";

export default function RoleLoadBar({ load }: { load: WeekLoadRow[] | null | undefined }) {
  const rows = (load || []).filter((r) => r.percent > 0);
  if (rows.length === 0) return null;

  return (
    <div className={styles.wrap}>
      <p className={styles.kicker}>This week&rsquo;s held load</p>
      <div
        className={styles.bar}
        role="img"
        aria-label={rows.map((r) => `${r.label} ${r.percent} percent`).join(", ")}
      >
        {rows.map((r) => (
          <span
            key={r.activity_type}
            className={styles.seg}
            style={{ width: `${Math.max(r.percent, 2)}%`, background: r.color }}
            title={`${r.label}: ${r.percent}%`}
          />
        ))}
      </div>
      <ul className={styles.legend}>
        {rows.map((r) => (
          <li key={r.activity_type}>
            <span className={styles.dot} style={{ background: r.color }} />
            <span className={styles.label}>{r.label}</span>
            <span className={styles.pct}>{r.percent}%</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
