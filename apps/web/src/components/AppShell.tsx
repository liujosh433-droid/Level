import AppNav from "./AppNav";
import styles from "./AppShell.module.css";

export default function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className={styles.root}>
      <AppNav />
      <div className={`${styles.shell} ${styles.dashboard}`}>
        <div className={styles.body}>{children}</div>
      </div>
    </div>
  );
}
