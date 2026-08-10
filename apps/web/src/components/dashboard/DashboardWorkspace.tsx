"use client";

import type { ReactNode } from "react";
import styles from "./DashboardWorkspace.module.css";

/** Shared Today/Profile (and future) two-column dashboard: main left, rail right. */
export function DashboardWorkspace({
  children,
  rail,
  railAriaLabel = "Level sidebar",
}: {
  children: ReactNode;
  rail: ReactNode;
  railAriaLabel?: string;
}) {
  return (
    <div className={styles.workspace}>
      <div className={styles.mainCol}>{children}</div>
      <aside className={styles.rail} aria-label={railAriaLabel}>
        {rail}
      </aside>
    </div>
  );
}

export function RailSection({
  title,
  children,
}: {
  title?: string;
  children: ReactNode;
}) {
  return (
    <section className={styles.railBlock}>
      {title ? <h2>{title}</h2> : null}
      {children}
    </section>
  );
}
