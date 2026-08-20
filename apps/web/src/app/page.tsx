"use client";

import Image from "next/image";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { resolveHomeDestination } from "@/lib/home";
import styles from "./page.module.css";

export default function HomePage() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    let cancelled = false;
    void resolveHomeDestination().then(({ dest }) => {
      if (cancelled) return;
      if (dest === "/today") {
        router.replace(dest);
        return;
      }
      setChecking(false);
    });
    return () => {
      cancelled = true;
    };
  }, [router]);

  function start() {
    router.push("/today");
  }

  if (checking) {
    return (
      <main className={`themeDark ${styles.hero}`}>
        <div className={styles.sky} aria-hidden="true">
          <div className={styles.wash} />
        </div>
      </main>
    );
  }

  return (
    <main className={`themeDark ${styles.hero}`}>
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
          <h1 className={styles.headline}>Steady when everything else isn&apos;t.</h1>
          <p className={styles.sub}>
            For busy caregivers &mdash; a partner that helps you organize your care-roles, priorities, and reminds
            you when usual events are missing. <br></br><br></br>Level can also send emails for you, like emailing kids' teachers when they need to take a sick day. Try it out now!
          </p>
          <div className={styles.cta}>
            <button type="button" className={styles.primary} onClick={start}>
              Get Started
            </button>
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
