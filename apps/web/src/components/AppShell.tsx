import type { ReactNode } from "react";
import { AppNav } from "./AppNav";
import styles from "./AppShell.module.css";

export function AppShell({
  userId,
  children,
  wide,
}: {
  /** When set, shows signed-in chrome (logout). Identity comes from the cookie. */
  userId?: string;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className={wide ? `${styles.shell} ${styles.wide}` : styles.shell}>
      <AppNav signedIn={Boolean(userId)} />
      <div className={styles.body}>{children}</div>
    </div>
  );
}
