import AppNav from "./AppNav";
import DataInspector from "./DataInspector";
import styles from "./AppShell.module.css";

export default function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <div className={styles.root}>
      <DataInspector />
      <div className={styles.main}>
        <AppNav />
        <div className={`${styles.shell} ${styles.dashboard}`}>
          <div className={styles.body}>{children}</div>
        </div>
      </div>
    </div>
  );
}
