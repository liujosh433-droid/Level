"use client";

import Image from "next/image";
import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ensureSession } from "@/lib/api";
import { resolveHomeDestination } from "@/lib/home";
import styles from "./welcome.module.css";

const BEATS = [
  {
    mark: "1",
    title: "Learns from your past",
    line: "Your real week day by day — not generic advice.",
  },
  {
    mark: "2",
    title: "Adapts for what’s next",
    line: "Today’s schedule and load, in view.",
  },
  {
    mark: "3",
    title: "Reasons with you",
    line: "The hard question before you commit.",
  },
] as const;

const HOW = [
  {
    icon: "cal",
    title: "Your busy schedule",
    body: "Auto-sync your Google Calendar — pickups, classes, work, and those nights that always run late.",
  },
  {
    icon: "chat",
    title: "Optional: ChatGPT chats",
    body: "You’ve probably asked ChatGPT for help with hard choices before. If you’d like, we can use those past questions so Level can help you take a step further with real knowledge of your lifestyle.",
  },
] as const;

export default function WelcomePage() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    let cancelled = false;
    void resolveHomeDestination().then(({ dest }) => {
      if (cancelled) return;
      // Already in — skip the pitch and go home (Today if Google is linked).
      if (dest === "/today" || dest === "/sources") {
        router.replace(dest);
        return;
      }
      setChecking(false);
    });
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function start() {
    try {
      // Mint/resume a cookie BEFORE Sources — otherwise Sources sees 401 and
      // bounces right back here ("Lets get started" loop).
      const me = await ensureSession();
      if (me.google_connected) {
        router.push("/today");
        return;
      }
      router.push("/sources");
    } catch {
      router.push("/sources");
    }
  }

  if (checking) {
    return (
      <main className={`themeDark ${styles.page}`}>
        <div className={styles.sky} aria-hidden="true">
          <div className={styles.wash} />
        </div>
      </main>
    );
  }

  return (
    <main className={`themeDark ${styles.page}`}>
      <div className={styles.sky} aria-hidden="true">
        <div className={styles.wash} />
        <span className={`${styles.mist} ${styles.mistA}`} />
        <span className={`${styles.mist} ${styles.mistB}`} />
        <div className={styles.horizon} />
      </div>

      <div className={styles.inner}>
        <header className={styles.top}>
          <Link href="/" className={styles.brand}>
            Level
          </Link>
        </header>

        <div className={styles.heroRow}>
          <div className={styles.copy}>
            <h1 className={styles.title}>Not another yes-man.</h1>
            <p className={styles.lead}>
              Being a single parent or busy caregiver is tough enough. You need a partner who
              actually knows your day-by-day challenges.
            </p>
            <p className={styles.contrast}>
              <span>Most AI cheers you on.</span>
              <span>
                Level stays <em>honest</em> about the hard calls.
              </span>
            </p>
          </div>

          <figure className={styles.figure}>
            <Image
              src="/welcome-compare.jpg"
              alt="Left: a yes-man chatbot cheering you on. Right: Level helping you stay steady with balance and your real calendar."
              width={1400}
              height={788}
              priority
              className={styles.compare}
            />
          </figure>
        </div>

        <ul className={styles.beats}>
          {BEATS.map((b) => (
            <li key={b.mark}>
              <span className={styles.mark} aria-hidden="true">
                {b.mark}
              </span>
              <div>
                <h2>{b.title}</h2>
                <p>{b.line}</p>
              </div>
            </li>
          ))}
        </ul>

        <section className={styles.how} aria-labelledby="how-heading">
          <div className={styles.howHead}>
            <h2 id="how-heading">How?</h2>
            <p>
              We connect a light slice of what you’re already juggling — so help fits your actual
              life.
            </p>
          </div>

          <div className={styles.howBoard}>
            <div className={styles.howArt} aria-hidden="true">
              <span className={styles.howGlow} />
              <Image
                src="/welcome-how.jpg"
                alt=""
                width={1200}
                height={675}
                className={styles.howImg}
              />
            </div>

            <ol className={styles.howList}>
              {HOW.map((item, i) => (
                <li key={item.title} style={{ animationDelay: `${0.08 + i * 0.08}s` }}>
                  <span
                    className={`${styles.howIcon} ${styles[`icon_${item.icon}`]}`}
                    aria-hidden="true"
                  />
                  <div>
                    <h3>{item.title}</h3>
                    <p>{item.body}</p>
                  </div>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <div className={styles.actions}>
          <button type="button" className={styles.primary} onClick={start}>
            Lets get started!
          </button>
        </div>
      </div>
    </main>
  );
}
