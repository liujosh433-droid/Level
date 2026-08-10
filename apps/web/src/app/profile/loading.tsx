import { AppShell } from "@/components/AppShell";
import styles from "./profile.module.css";

/** Instant route shell — don’t wait for profile data before painting. */
export default function ProfileLoading() {
  return (
    <AppShell dashboard>
      <h1 className={styles.title}>Your Priorities</h1>
      <p className={styles.meta}>Loading your priorities…</p>
    </AppShell>
  );
}
