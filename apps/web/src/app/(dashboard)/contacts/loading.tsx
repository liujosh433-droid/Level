import { AppShell } from "@/components/AppShell";
import styles from "./contacts.module.css";

export default function ContactsLoading() {
  return (
    <AppShell dashboard contentOnly>
      <div className={styles.page}>
        <h1 className={styles.title}>Contacts</h1>
        <p className={styles.meta}>Loading…</p>
      </div>
    </AppShell>
  );
}
