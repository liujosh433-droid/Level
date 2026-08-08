"use client";

import { FormEvent, Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  createDecision,
  fetchProfile,
  takeTurn,
  type Profile,
  type Turn,
} from "@/lib/api";
import styles from "./session.module.css";

const DEMO_PROMPT =
  "I think I should apply for the promotion. Everyone says I'd be great at it. " +
  "And maybe switch Maya to the dual-language school at the same time — her friend is going.";

function SessionPageInner() {
  const searchParams = useSearchParams();
  const isDemo = searchParams.get("demo") === "1";

  const [userId, setUserId] = useState("");
  const [decisionId, setDecisionId] = useState<string | null>(null);
  // Always start empty so SSR HTML matches the first client render; fill demo
  // prompt after mount to avoid hydration mismatch.
  const [draft, setDraft] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);

  useEffect(() => {
    const fromUrl = searchParams.get("user_id");
    const stored =
      typeof window !== "undefined" ? localStorage.getItem("level_user_id") : null;
    const id = fromUrl || stored || (isDemo ? "demo-parent" : "");
    if (fromUrl) localStorage.setItem("level_user_id", fromUrl);
    setUserId(id);
  }, [searchParams, isDemo]);

  useEffect(() => {
    if (isDemo) {
      setDraft(DEMO_PROMPT);
    }
  }, [isDemo]);

  useEffect(() => {
    if (!userId) return;
    void fetchProfile(userId)
      .then(setProfile)
      .catch(() => setProfile(null));
  }, [userId, turns.length]);

  async function ensureDecision(): Promise<string> {
    if (decisionId) return decisionId;
    const d = await createDecision(userId);
    setDecisionId(d.decision_id);
    return d.decision_id;
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!draft.trim() || busy || !userId) return;
    setBusy(true);
    setError(null);
    try {
      const id = await ensureDecision();
      const turn = await takeTurn(id, userId, draft.trim());
      setTurns((prev) => [...prev, turn]);
      setDraft("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const topBiases = (profile?.bias_scores ?? []).filter((s) => s.ema >= 0.2).slice(0, 5);

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <Link href="/" className={styles.brand}>
          Level
        </Link>
        <nav className={styles.nav}>
          <Link href={userId ? `/sources?user_id=${userId}` : "/sources"}>Sources</Link>
          <p className={styles.meta}>
            {decisionId ? `Decision ${decisionId.slice(0, 8)}…` : "New decision"}
            {" · "}
            {userId ? (userId === "demo-parent" ? "demo-parent" : `${userId.slice(0, 8)}…`) : "no user"}
          </p>
        </nav>
      </header>

      {!userId && (
        <p className={styles.banner}>
          Connect your sources first so Level has real evidence to cite.{" "}
          <Link href="/sources">Go to Sources →</Link>
        </p>
      )}

      {profile && profile.fact_count > 0 && (
        <aside className={styles.biasPanel} aria-label="Bias profile">
          <div className={styles.biasHead}>
            <span>{profile.fact_count} facts in memory</span>
            {topBiases.length > 0 && <span>Active patterns</span>}
          </div>
          {topBiases.length > 0 && (
            <ul className={styles.biasList}>
              {topBiases.map((b) => (
                <li key={b.category}>
                  <span>{b.category.replaceAll("_", " ")}</span>
                  <meter max={1} value={b.ema} />
                </li>
              ))}
            </ul>
          )}
          {profile.manifesto && (
            <p className={styles.manifesto}>{profile.manifesto}</p>
          )}
        </aside>
      )}

      <section className={styles.thread} aria-live="polite">
        {turns.length === 0 && (
          <p className={styles.empty}>
            Bring a real decision. Level will ask the hard clarifying question —
            and cite your own past when it can.
          </p>
        )}
        {turns.map((turn) => (
          <article key={turn.turn_id} className={styles.turn}>
            {turn.user_text ? (
              <div className={styles.you}>
                <span className={styles.who}>You</span>
                <p>{turn.user_text}</p>
              </div>
            ) : null}
            <div className={styles.level}>
              <span className={styles.who}>Level</span>
              {turn.status === "blocked" || turn.status === "degraded" ? (
                <p className={styles.degraded}>
                  I hit a snag ({turn.status}
                  {turn.degradation_reason ? `: ${turn.degradation_reason}` : ""}).
                  Try again, or give me more context.
                </p>
              ) : (
                turn.challenger_questions.map((q, i) => (
                  <div key={i} className={styles.question}>
                    <p>{q.question}</p>
                    {q.citations.length > 0 && (
                      <ul className={styles.cites}>
                        {q.citations.map((c) => (
                          <li key={c.fact_id}>
                            <span className={styles.citeLabel}>{q.challenge_type}</span>
                            {c.quote}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                ))
              )}
            </div>
          </article>
        ))}
      </section>

      <form className={styles.composer} onSubmit={onSubmit}>
        {error && <p className={styles.error}>{error}</p>}
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="What's the decision you're sitting with?"
          rows={4}
          disabled={busy || !userId}
        />
        <div className={styles.actions}>
          <button type="submit" disabled={busy || !draft.trim() || !userId}>
            {busy ? "Listening…" : "Ask Level"}
          </button>
        </div>
      </form>
    </main>
  );
}

export default function SessionPage() {
  return (
    <Suspense fallback={<main className={styles.page} />}>
      <SessionPageInner />
    </Suspense>
  );
}
