import { AppShell } from "@/components/AppShell";
import styles from "./today.module.css";

export default function TodayLoading() {
  return (
    <AppShell dashboard>
      <p className={styles.bootMsg}>Loading your day…</p>
    </AppShell>
  );
}
