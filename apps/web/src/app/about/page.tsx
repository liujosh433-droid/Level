"use client";

import Image from "next/image";
import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { fetchMe } from "@/lib/api";
import styles from "./about.module.css";

function AboutInner() {
  const [userId, setUserId] = useState("");
  const [displayName, setDisplayName] = useState<string | null>(null);

  useEffect(() => {
    void fetchMe()
      .then((me) => {
        setUserId(me.user_id);
        setDisplayName(me.display_name);
      })
      .catch(() => {
        // Readable signed-out; ignore auth / network failures.
      });
  }, []);

  return (
    <AppShell userId={userId || undefined} displayName={displayName} dashboard>
      <div className={styles.layout}>
        <aside className={styles.artCol} aria-hidden="true">
          <figure className={styles.artFrame}>
            <Image
              src="/about-voice.jpg"
              alt=""
              width={1200}
              height={900}
              priority
              className={styles.artImg}
            />
            <figcaption className={styles.artCaption}>Speak while you move</figcaption>
          </figure>
          <figure className={`${styles.artFrame} ${styles.artFrameDelay}`}>
            <Image
              src="/about-listen.jpg"
              alt=""
              width={1200}
              height={900}
              className={styles.artImg}
            />
            <figcaption className={styles.artCaption}>Hear your day while driving</figcaption>
          </figure>
        </aside>

        <div className={styles.copyCol}>
          <figure className={`${styles.artFrame} ${styles.mobileArt}`}>
            <Image
              src="/about-voice.jpg"
              alt=""
              width={1200}
              height={900}
              priority
              className={styles.artImg}
            />
          </figure>

          <h1 className={styles.title}>What Level can do</h1>
          <p className={styles.lead}>
            Level is your decision partner for a crowded life. It learns what you protect, helps you
            prepare for the day, and weighs hard calls against your{" "}
            <span className={styles.accent}>real priorities</span> — not generic advice.
          </p>

          <section className={styles.section}>
            <h2>Learns your priorities</h2>
            <p>
              Connect your calendar (and optionally past ChatGPT conversations). Level doesn’t just
              list events — it <span className={styles.accent}>interprets</span> what they say you
              care about: family time, protected work hours, visits with Mom, sleep and recovery, and
              more.
            </p>
          </section>

          <section className={styles.section}>
            <h2>Adjusts with your feedback</h2>
            <p>
              When you tap <span className={styles.ui}>Keep</span> or{" "}
              <span className={styles.ui}>Not me</span>, or add something in{" "}
              <span className={styles.ui}>Tell Level more</span>, Level reshapes what it treats as
              true. Over time it learns what helps you thrive.
            </p>
          </section>

          <section className={styles.section}>
            <h2>Talk hands-free — and hear your day</h2>
            <p>
              Speak to Level while you multitask — packing bags, making lunch, or on the move.
              Level can also speak back: tap <span className={styles.ui}>Hear my day</span> on Today
              for a spoken briefing when you’re driving or can’t look at the screen.
            </p>
          </section>

          <section className={styles.section}>
            <h2>Books time — and pushes back</h2>
            <p>
              Ask Level to put something on your calendar and it can propose slots that fit. If a new
              ask <span className={styles.accent}>conflicts</span> with what you’ve taught it
              matters, it will say so instead of quietly saying yes.
            </p>
            <p className={styles.example}>
              <strong>Example:</strong> You ask to stack a late Thursday networking dinner. Level
              sees Jordan’s soccer that evening and your priority to protect{" "}
              <span className={styles.accent}>family weeknights</span> — and offers other times, or
              asks if you’re sure, rather than double-booking on autopilot.
            </p>
          </section>

          <section className={styles.section}>
            <h2>Remembers so you’re ready</h2>
            <p>
              Tell Level once — don’t forget soccer shoes, Mom needs a quiet visit, that clinic
              always runs late — and it surfaces those reminders when the day comes around. Past
              experiences stick around so you’re better prepared next time, not starting from
              scratch.
            </p>
          </section>

          <div className={styles.ctaRow}>
            {userId ? (
              <>
                <Link href="/today" className={styles.cta}>
                  Back to Today
                </Link>
                <Link href="/profile" className={styles.ghost}>
                  Review priorities
                </Link>
              </>
            ) : (
              <>
                <Link href="/welcome" className={styles.cta}>
                  Get started
                </Link>
                <Link href="/" className={styles.ghost}>
                  Home
                </Link>
              </>
            )}
          </div>
        </div>
      </div>
    </AppShell>
  );
}

export default function AboutPage() {
  return (
    <Suspense
      fallback={
        <AppShell dashboard>
          <p className={styles.lead}>Loading…</p>
        </AppShell>
      }
    >
      <AboutInner />
    </Suspense>
  );
}
