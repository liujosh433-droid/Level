"use client";

import Image from "next/image";
import { FormEvent, Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { AuthError, ensureSession, fetchGoogleSyncStatus, fetchMe, getApiBase } from "@/lib/api";
import styles from "./sources.module.css";

const CHATGPT_STEPS = [
  "In ChatGPT, open Settings → Personalization → Memory (or ask: “What do you remember about me?”).",
  "Copy the memory summary — especially family, work, and caregiving bits.",
  "Paste it below. Level keeps what’s relevant to your care roles.",
] as const;

const SYNC_BEATS = [
  { at: 0, label: "Saying hi to Google…", progress: 12 },
  { at: 3, label: "Reading your calendar…", progress: 34 },
  { at: 8, label: "Noticing the shape of your week…", progress: 58 },
  { at: 16, label: "Putting your profile together…", progress: 78 },
  { at: 28, label: "Almost ready — hang tight…", progress: 90 },
] as const;

type Phase = "connect" | "syncing" | "addons";

function syncBeatForElapsed(seconds: number): (typeof SYNC_BEATS)[number] {
  let beat = SYNC_BEATS[0];
  for (const b of SYNC_BEATS) {
    if (seconds >= b.at) beat = b;
  }
  return beat;
}

function SourcesInner() {
  const params = useSearchParams();
  const router = useRouter();
  const [userId, setUserId] = useState("");
  const [email, setEmail] = useState<string | null>(null);
  const [googleConnected, setGoogleConnected] = useState(false);
  const [canWriteCalendar, setCanWriteCalendar] = useState(true);
  const [canSendEmail, setCanSendEmail] = useState(true);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState<Phase>("connect");
  const [showHowto, setShowHowto] = useState(false);
  const [ready, setReady] = useState(false);
  const [syncElapsed, setSyncElapsed] = useState(0);
  const [syncProgress, setSyncProgress] = useState(10);
  const [syncLabel, setSyncLabel] = useState(SYNC_BEATS[0].label);
  const [memoryPaste, setMemoryPaste] = useState("");

  useEffect(() => {
    const fromOAuth = params.get("connected") === "1";
    const needGmail = params.get("need_gmail") === "1";
    let cancelled = false;

    void (async () => {
      try {
        // Don't auto-create a guest here — that was minting new users and
        // bouncing already-linked caregivers back to "Connect Google".
        const me = await fetchMe();
        if (cancelled) return;
        setUserId(me.user_id);
        setEmail(me.email);
        setGoogleConnected(me.google_connected || fromOAuth);
        setCanWriteCalendar(me.can_write_calendar !== false);
        setCanSendEmail(me.can_send_email !== false);
        if (needGmail && me.can_send_email === false) {
          setStatus(
            "Google still didn’t attach Send email. Enable the Gmail API and add gmail.send on the OAuth consent screen, then tap Allow sending school notes and leave Send email checked.",
          );
        }

        if (fromOAuth) {
          setPhase("syncing");
          setBusy(true);
          setReady(true);
          setSyncElapsed(0);
          setSyncProgress(SYNC_BEATS[0].progress);
          setSyncLabel(SYNC_BEATS[0].label);
          const started = Date.now();
          while (!cancelled && Date.now() - started < 90_000) {
            const elapsedSec = Math.floor((Date.now() - started) / 1000);
            const beat = syncBeatForElapsed(elapsedSec);
            setSyncElapsed(elapsedSec);
            try {
              const st = await fetchGoogleSyncStatus();
              let progress = beat.progress;
              let label = beat.label;
              if (st.agenda_event_count > 0) {
                progress = Math.max(progress, 62);
                label = "Calendar’s in — finishing your profile…";
              }
              if (st.profile_ingested) {
                progress = Math.max(progress, 92);
                label = "Profile ready — taking you to Today…";
              }
              setSyncProgress(progress);
              setSyncLabel(label);
              if (st.initial_sync_done) {
                if (cancelled) return;
                setSyncProgress(100);
                setSyncLabel("You’re set — opening Today…");
                await new Promise((r) => setTimeout(r, 450));
                router.replace("/today");
                return;
              }
            } catch {
              setSyncProgress(beat.progress);
              setSyncLabel(beat.label);
            }
            await new Promise((r) => setTimeout(r, 900));
          }
          if (!cancelled) {
            router.replace("/today");
          }
          return;
        }

        if (me.google_connected) {
          // Returning user opened Sources intentionally → optional add-ons.
          setPhase("addons");
          setReady(true);
          return;
        }

        setPhase("connect");
        setReady(true);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof AuthError) {
          // First-time / expired cookie: create a guest and show Connect.
          // (fetchMe already retried — this is a real missing session.)
          try {
            const guest = await ensureSession();
            if (cancelled) return;
            setUserId(guest.user_id);
            setGoogleConnected(Boolean(guest.google_connected));
            setCanWriteCalendar(guest.can_write_calendar !== false);
            setCanSendEmail(guest.can_send_email !== false);
            setPhase(guest.google_connected ? "addons" : "connect");
            setReady(true);
            return;
          } catch (inner) {
            setStatus(inner instanceof Error ? inner.message : String(inner));
            setReady(true);
            return;
          }
        }
        setStatus(err instanceof Error ? err.message : String(err));
        setReady(true);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [params, router]);

  async function connectGoogle(need?: "gmail") {
    setBusy(true);
    setStatus(null);
    try {
      await ensureSession();
      const q = need === "gmail" ? "?need=gmail" : "";
      window.location.href = `${getApiBase()}/v1/auth/google/start${q}`;
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  async function onChatGPT(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const text = memoryPaste.trim();
    if (text.length < 20) {
      setStatus("Paste a longer ChatGPT Memory summary first.");
      return;
    }
    setBusy(true);
    setStatus(null);
    try {
      await ensureSession();
      const res = await fetch(`${getApiBase()}/v1/sources/chatgpt`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
        credentials: "include",
      });
      const raw = await res.text();
      let data: { detail?: string } = {};
      try {
        data = raw ? JSON.parse(raw) : {};
      } catch {
        throw new Error(raw.trim() || `Request failed (${res.status})`);
      }
      if (!res.ok) {
        const detail = data.detail;
        throw new Error(
          typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : raw || `Request failed (${res.status})`,
        );
      }
      setStatus((data as { detail?: string }).detail || "ChatGPT Memory added.");
      setMemoryPaste("");
      setShowHowto(false);
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  if (!ready) {
    return (
      <AppShell userId={userId || undefined} dashboard contentOnly>
        <div className={styles.wrap}>
          <section className={`${styles.panel} ${styles.syncPanel}`}>
            <div className={styles.syncCard}>
              <div className={styles.syncSpinner} aria-hidden>
                <span />
              </div>
              <p className={styles.syncLabel}>Just a sec…</p>
            </div>
          </section>
        </div>
      </AppShell>
    );
  }

  return (
    <AppShell userId={userId || undefined} dashboard contentOnly>
      <div className={styles.wrap}>
        {phase === "connect" && (
          <section className={`${styles.panel} ${styles.enter}`} key="connect">
            <p className={styles.kicker}>Get started</p>
            <h1 className={styles.title}>Sync your week</h1>
            <p className={styles.intro}>
              Connect Google Calendar so Level can see your usual week.
            </p>

            <aside className={styles.consentTip}>
              <div className={styles.consentCopy}>
                <p className={styles.consentTitle}>On Google’s next screen</p>
                <p>
                  Check every box Level asks for — <strong>Calendar</strong> and{" "}
                  <strong>sending email</strong> for school or clinic notes — then tap{" "}
                  <strong>Continue</strong>. If sending email is unchecked, Level can see
                  your week but can’t send the teacher a note.
                </p>
              </div>
              <figure className={styles.consentArt}>
                <Image
                  src="/google-consent-tip.jpg"
                  alt="Example: check Calendar access for Level, then Continue"
                  width={1280}
                  height={720}
                  className={styles.consentImg}
                />
              </figure>
            </aside>

            <button
              type="button"
              className={styles.primary}
              disabled={busy}
              onClick={() => void connectGoogle()}
            >
              {busy ? "Starting…" : "Connect Google"}
            </button>
          </section>
        )}

        {phase === "syncing" && (
          <section
            className={`${styles.panel} ${styles.syncPanel} ${styles.enter}`}
            key="syncing"
            aria-busy="true"
            aria-live="polite"
          >
            <p className={styles.kicker}>Almost there</p>
            <h1 className={styles.title}>Pulling in your week</h1>
            <p className={styles.line}>
              {email ? `Linked as ${email}.` : "Google is linked."} Grab a breath — Level is
              learning your real schedule, not a generic template.
            </p>

            <div className={styles.syncCard}>
              <div className={styles.syncSpinner} aria-hidden>
                <span />
              </div>
              <p className={styles.syncLabel}>{syncLabel}</p>
              <div
                className={styles.progressTrack}
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={syncProgress}
                aria-label="Google sync progress"
              >
                <div
                  className={styles.progressFill}
                  style={{ width: `${syncProgress}%` }}
                />
              </div>
              <p className={styles.syncHint}>
                Usually under a minute
                {syncElapsed > 20 ? " — still working, almost there" : ""}
              </p>
            </div>
          </section>
        )}

        {phase === "addons" && (
          <section className={`${styles.panel} ${styles.enter}`} key="addons">
            <h1 className={styles.title}>Add more context</h1>
            <p className={styles.intro}>
              {email ? `Calendar linked as ${email}.` : "Google Calendar is linked."} Optional
              extras below — skip anytime.
            </p>
            {(!canWriteCalendar || !canSendEmail) && (
              <aside className={styles.consentTip}>
                <div className={styles.consentCopy}>
                  <p className={styles.consentTitle}>One more Google permission</p>
                  <p>
                    {!canSendEmail
                      ? "Level can see your calendar, but Google still hasn’t granted sending email. Tap the button below — on Google’s next screen, leave Send email checked."
                      : "Calendar is read-only right now. Reconnect and allow Level to edit events."}
                  </p>
                </div>
              </aside>
            )}

            <div className={styles.addonList}>
              <div className={styles.addon}>
                <div>
                  <h2>ChatGPT Memory</h2>
                  <p>Paste what ChatGPT already knows about your life.</p>
                </div>
                <button
                  type="button"
                  className={styles.ghost}
                  onClick={() => {
                    setShowHowto((v) => !v);
                    setMemoryPaste("");
                  }}
                >
                  {showHowto ? "Hide" : "Paste memory"}
                </button>
              </div>
            </div>

            {showHowto && (
              <div className={`${styles.howtoWrap} ${styles.enter}`}>
                <ol className={styles.howto}>
                  {CHATGPT_STEPS.map((line, i) => (
                    <li key={line} style={{ animationDelay: `${0.05 + i * 0.1}s` }}>
                      <span>{i + 1}</span>
                      <p>{line}</p>
                    </li>
                  ))}
                </ol>
                <form onSubmit={onChatGPT} className={styles.upload}>
                  <label className={styles.pasteLabel} htmlFor="chatgpt-memory">
                    Memory summary
                  </label>
                  <textarea
                    id="chatgpt-memory"
                    name="memory"
                    className={styles.pasteArea}
                    rows={8}
                    value={memoryPaste}
                    onChange={(e) => setMemoryPaste(e.target.value)}
                    placeholder="Paste your ChatGPT Memory summary here…"
                    required
                    minLength={20}
                    maxLength={24000}
                  />
                  <div className={styles.uploadRow}>
                    <button
                      type="submit"
                      className={styles.uploadBtn}
                      disabled={busy || memoryPaste.trim().length < 20}
                    >
                      {busy ? "Reading…" : "Add to Level"}
                    </button>
                  </div>
                  <p className={styles.fileName}>
                    Level extracts care-relevant facts only — not your whole chat history.
                  </p>
                </form>
              </div>
            )}

            <div className={styles.actions}>
              <button
                type="button"
                className={styles.primary}
                onClick={() => router.push("/today")}
              >
                Go to Today
              </button>
              {(!canWriteCalendar || !canSendEmail) && (
                <button
                  type="button"
                  className={styles.soft}
                  disabled={busy}
                  onClick={() =>
                    void connectGoogle(canSendEmail ? undefined : "gmail")
                  }
                >
                  {canSendEmail
                    ? "Update Google permissions"
                    : "Allow sending school notes"}
                </button>
              )}
            </div>
          </section>
        )}

        {status && <p className={styles.status}>{status}</p>}
      </div>
    </AppShell>
  );
}

export default function SourcesPage() {
  return (
    <Suspense
      fallback={
        <AppShell dashboard contentOnly>
          <p className={styles.line}>Loading…</p>
        </AppShell>
      }
    >
      <SourcesInner />
    </Suspense>
  );
}
