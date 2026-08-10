"use client";

import { FormEvent, Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { API_BASE, AuthError, ensureSession, fetchMe } from "@/lib/api";
import styles from "./sources.module.css";

const DRIVE_KEY = "level_include_drive";

const CHATGPT_STEPS = [
  "Open ChatGPT and tap your profile picture.",
  "Go to Settings → Data controls → Export data.",
  "Open the email, download the file, then choose it here.",
] as const;

function SourcesInner() {
  const params = useSearchParams();
  const router = useRouter();
  const [userId, setUserId] = useState("");
  const [email, setEmail] = useState<string | null>(null);
  const [googleConnected, setGoogleConnected] = useState(false);
  const [includeDrive, setIncludeDrive] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState<"google" | "chatgpt" | "done">("google");
  const [showHowto, setShowHowto] = useState(false);

  useEffect(() => {
    const stored = localStorage.getItem(DRIVE_KEY);
    if (stored != null) setIncludeDrive(stored === "1");
  }, []);

  useEffect(() => {
    if (params.get("connected") === "1") {
      setGoogleConnected(true);
    }
    void ensureSession("Caregiver")
      .then((me) => {
        setUserId(me.user_id);
        setEmail(me.email);
        setGoogleConnected(me.google_connected || params.get("connected") === "1");
      })
      .catch((err) => setStatus(err instanceof Error ? err.message : String(err)));
  }, [params]);

  function toggleDrive(next: boolean) {
    setIncludeDrive(next);
    localStorage.setItem(DRIVE_KEY, next ? "1" : "0");
  }

  async function connectGoogle() {
    setBusy(true);
    setStatus(null);
    try {
      await ensureSession("Caregiver");
      window.location.href = `${API_BASE}/v1/auth/google/start`;
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  async function syncGoogle() {
    setBusy(true);
    setStatus(null);
    try {
      await ensureSession("Caregiver");
      const body = new FormData();
      body.set("include_drive", includeDrive ? "true" : "false");
      const res = await fetch(`${API_BASE}/v1/sources/google/sync`, {
        method: "POST",
        body,
        credentials: "include",
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      setPhase("chatgpt");
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function onChatGPT(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setStatus(null);
    try {
      await ensureSession("Caregiver");
      const input = e.currentTarget.elements.namedItem("export") as HTMLInputElement;
      const file = input?.files?.[0];
      if (!file) return;
      const body = new FormData();
      body.set("file", file);
      body.set("max_messages", "40");
      const res = await fetch(`${API_BASE}/v1/sources/chatgpt`, {
        method: "POST",
        body,
        credentials: "include",
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
      setPhase("done");
      router.push("/profile");
    } catch (err) {
      setStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  function goProfile() {
    router.push("/profile");
  }

  // Refresh me after OAuth return
  useEffect(() => {
    if (params.get("connected") !== "1") return;
    void fetchMe()
      .then((me) => {
        setUserId(me.user_id);
        setEmail(me.email);
        setGoogleConnected(me.google_connected);
      })
      .catch((err) => {
        if (!(err instanceof AuthError)) {
          setStatus(err instanceof Error ? err.message : String(err));
        }
      });
  }, [params]);

  const progress = phase === "google" ? 1 : 2;

  return (
    <AppShell userId={userId || undefined} wide>
      <div className={styles.wrap}>
        <div className={styles.progress} aria-label={`Step ${progress} of 2`}>
          {[1, 2].map((n) => (
            <span
              key={n}
              className={n <= progress ? `${styles.dot} ${styles.dotOn}` : styles.dot}
            />
          ))}
        </div>

        {phase === "google" && (
          <section className={`${styles.panel} ${styles.enter}`} key="google">
            <p className={styles.kicker}>Step 1/2</p>
            <h1 className={styles.title}>Sync your week</h1>
            <p className={styles.intro}>
              Let’s get started with your Google Calendar — so Level can see your usual week.
            </p>

            <label className={styles.check}>
              <input
                type="checkbox"
                checked={includeDrive}
                onChange={(e) => toggleDrive(e.target.checked)}
              />
              <span>
                Also include related Google Drive notes
                <small>Optional — only files tied to your schedule</small>
              </span>
            </label>

            {!googleConnected ? (
              <button
                type="button"
                className={styles.primary}
                disabled={busy}
                onClick={() => void connectGoogle()}
              >
                {busy ? "Starting…" : "Connect Google"}
              </button>
            ) : (
              <>
                <p className={styles.line}>
                  {email ? `Linked as ${email}.` : "Google is linked."} Ready when you are.
                </p>
                <button
                  type="button"
                  className={styles.primary}
                  disabled={busy}
                  onClick={() => void syncGoogle()}
                >
                  {busy ? "Working…" : "Bring in my week"}
                </button>
                <button
                  type="button"
                  className={styles.soft}
                  disabled={busy}
                  onClick={() => void connectGoogle()}
                >
                  Reconnect Google (needed to add events from Level)
                </button>
              </>
            )}
          </section>
        )}

        {phase === "chatgpt" && (
          <section className={`${styles.panel} ${styles.enter}`} key="chatgpt">
            <p className={styles.kicker}>Step 2/2</p>
            <h1 className={styles.title}>Add ChatGPT?</h1>
            <p className={styles.line}>
              Optional — only if you’ve used it for hard choices before. Skip anytime.
            </p>

            <div className={styles.actions}>
              <button
                type="button"
                className={styles.ghost}
                onClick={() => setShowHowto((v) => !v)}
              >
                {showHowto ? "Hide steps" : "Show me how"}
              </button>
              <button type="button" className={styles.primary} onClick={goProfile}>
                Skip for now
              </button>
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
                  <label className={styles.fileLabel}>
                    Choose the file from your email
                    <input
                      name="export"
                      type="file"
                      accept=".zip,.json,application/zip,application/json"
                      required
                    />
                  </label>
                  <button type="submit" className={styles.primary} disabled={busy}>
                    {busy ? "Uploading…" : "Upload"}
                  </button>
                </form>
              </div>
            )}
          </section>
        )}

        {phase === "done" && (
          <section className={`${styles.panel} ${styles.enter}`} key="done">
            <p className={styles.kicker}>Ready</p>
            <h1 className={styles.title}>You’re set</h1>
            <button type="button" className={styles.primary} onClick={goProfile}>
              See my profile
            </button>
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
        <AppShell wide>
          <p className={styles.line}>Loading…</p>
        </AppShell>
      }
    >
      <SourcesInner />
    </Suspense>
  );
}
