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
            You’re a busy caregiver. Without Level, the week lives in your head — who you hold,
            what a yes would crowd out, the paper still on the counter. Level holds that with you, and all
            you need to start with is linking your calendar. 
          
          </p>

          <section className={styles.section}>
            <h2>You forgot. Level didn’t.</h2>
            <p>
              It learns your repeating care from the calendar — that pickup, that clinic, that
              after-school run. Accidents happen - forgotten events, last-minute changes, mis-clicks. If the usual isn’t there this week, Level asks whether it was an
              accident: <span className={styles.ui}>Put it back</span>,{" "}
              <span className={styles.ui}>This week is different</span>, or{" "}
              <span className={styles.ui}>Not me</span>. You still decide. Level won’t invent who
              covers it.
            </p>
          </section>

          <section className={styles.section}>
            <h2>Names the squeeze</h2>
            <p>
              A new "yes" to an event that conflicts with your principles and priorities gets challenged, not quietly booked. You still
              decide. Level guides you and helps you maintain your values despite the busy schedule.
            </p>
          </section>

          <section className={styles.section}>
            <h2>Does the school chore</h2>
            <p>
              On Contacts, save you, each kid, and each person in elder care — plus who to
              email for them (teacher, doctor, or a type you add). Then ask Level in chat:
              email her teacher that she’s home sick, or that you need to send a slip.
              Level drafts the note, you edit the preview, and you{" "}
              <span className={styles.ui}>Send</span>. Institution only — never a friend text.
            </p>
          </section>

          <section className={styles.section}>
            <h2>Talks while you move</h2>
            <p>
              Speak to Level with your hands full.{" "}
              <span className={styles.ui}>Hear my day</span> reads the briefing aloud. Tell it once
              — shoes, a quiet visit, a clinic that runs late — and it reminds you when that day
              comes back.
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
