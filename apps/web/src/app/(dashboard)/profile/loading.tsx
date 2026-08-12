import styles from "./profile.module.css";

/** Instant route shell — don’t wait for profile data before painting. */
export default function ProfileLoading() {
  return (
    <>
      <h1 className={styles.title}>About me</h1>
      <p className={styles.meta}>Loading about you…</p>
    </>
  );
}
