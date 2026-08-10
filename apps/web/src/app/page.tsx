"use client";

import Image from "next/image";
import Link from "next/link";
import styles from "./page.module.css";

export default function HomePage() {
  return (
    <main className={styles.hero}>
      <div className={styles.sky} aria-hidden="true">
        <div className={styles.wash} />
        <span className={`${styles.mist} ${styles.mistA}`} />
        <span className={`${styles.mist} ${styles.mistB}`} />
        <span className={`${styles.mist} ${styles.mistC}`} />
        <div className={styles.horizon} />
        <div className={styles.sheen} />
      </div>

      <div className={styles.inner}>
        <div className={styles.copy}>
          <p className={styles.brand}>Level</p>
          <h1 className={styles.headline}>Steady when everything else isn’t.</h1>
          <p className={styles.sub}>
            For busy caregivers and single parents — honest help with the hard calls.
          </p>
          <div className={styles.cta}>
            <Link href="/welcome" className={styles.primary}>
              Get Started
            </Link>
            <p className={styles.hint}>We’ll explain first — then a quick connect</p>
          </div>
        </div>

        <div className={styles.art} aria-hidden="true">
          <span className={`${styles.orb} ${styles.orbA}`} />
          <span className={`${styles.orb} ${styles.orbB}`} />
          <span className={`${styles.orb} ${styles.orbC}`} />
          <div className={styles.artFrame}>
            <Image
              src="/hero-people.jpg"
              alt=""
              width={1200}
              height={675}
              priority
              className={styles.people}
            />
          </div>
        </div>
      </div>
    </main>
  );
}
