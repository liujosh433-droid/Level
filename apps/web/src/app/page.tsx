import Link from "next/link";
import styles from "./page.module.css";

export default function HomePage() {
  return (
    <main className={styles.hero}>
      <div className={styles.horizon} aria-hidden="true" />
      <div className={styles.inner}>
        <p className={styles.brand}>Level</p>
        <h1 className={styles.headline}>Keeps you level when everything else isn&apos;t.</h1>
        <p className={styles.sub}>
          A warm decision partner for busy caregivers — one that cites your own past
          and isn&apos;t afraid to ask the hard clarifying question.
        </p>
        <div className={styles.cta}>
          <Link href="/sources" className={styles.primary}>
            Connect your sources
          </Link>
          <Link href="/session?demo=1" className={styles.ghost}>
            Try the demo narrative
          </Link>
        </div>
      </div>
    </main>
  );
}
