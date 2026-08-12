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
    <AppShell userId={userId || undefined} displayName={displayName} dashboard contentOnly>
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
            Level doesn’t help caregivers find more hours — and it won’t preach balance or
            self-care. It keeps your <span className={styles.accent}>care roles</span> alive and
            asks, at decision time, what saying yes would crowd out — so you choose with eyes open, not
            more guilt.
          </p>

          <section className={styles.section}>
            <h2>Learns your care roles</h2>
            <p>
              Connect your calendar (and optionally paste ChatGPT Memory). Level doesn’t just
              list events — it builds a Care Profile: child care, elder care, paid work, household
              logistics, self &amp; recovery, and co-parent when present — with sticky windows like
              pickup.
            </p>
          </section>

          <section className={styles.section}>
            <h2>Adjusts with your feedback</h2>
            <p>
              When you tap <span className={styles.ui}>Keep</span> or{" "}
              <span className={styles.ui}>Not me</span>, or add something in{" "}
              <span className={styles.ui}>Tell Level more</span>, Level reshapes which roles it
              holds. No one-size “good parent” script.
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
            <h2>Challenges care collisions — and pushes back</h2>
            <p>
              Ask Level about a hard yes, or ask it to put something on your calendar. If the ask
              would <span className={styles.accent}>crowd out a care role</span> you marked Keep,
              Level names the squeeze instead of quietly agreeing.
            </p>
            <p className={styles.example}>
              <strong>Example:</strong> You ask to stack a late Thursday networking dinner. Level
              sees you marked Jordan’s pickup as Keep — child care — and asks how that yes doesn’t
              cut into that window, or offers other times. You still decide; Level won’t lecture.
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
                  About me
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
        <AppShell dashboard contentOnly>
          <p className={styles.lead}>Loading…</p>
        </AppShell>
      }
    >
      <AboutInner />
    </Suspense>
  );
}
