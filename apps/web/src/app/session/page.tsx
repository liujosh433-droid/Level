"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import {
  AuthError,
  createDecision,
  ensureSession,
  fetchProfile,
  takeTurn,
  type Profile,
  type Turn,
} from "@/lib/api";
import styles from "./session.module.css";

function SessionPageInner() {
  const router = useRouter();

  const [userId, setUserId] = useState("");
  const [decisionId, setDecisionId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const me = await ensureSession();
        setUserId(me.user_id);
        void fetchProfile()
          .then(setProfile)
          .catch(() => setProfile(null));
      } catch {
        router.replace("/welcome");
      }
    })();
  }, [router]);

  async function ensureDecision(): Promise<string> {
    if (decisionId) return decisionId;
    const d = await createDecision();
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
      const turn = await takeTurn(id, draft.trim());
      setTurns((prev) => [...prev, turn]);
      setDraft("");
      void fetchProfile()
        .then(setProfile)
        .catch(() => undefined);
    } catch (err) {
      if (err instanceof AuthError) {
        router.replace("/welcome");
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const topBiases = (profile?.bias_scores ?? []).filter((s) => s.ema >= 0.2).slice(0, 5);

  return (
    <AppShell userId={userId || undefined} wide>
      <p className={styles.meta}>
        Prefer the Ask box on <Link href="/today">Today</Link>
        {decisionId ? ` · Decision ${decisionId.slice(0, 8)}…` : ""}
      </p>

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
            <span>{profile.session_count} sessions</span>
          </div>
          {topBiases.length > 0 && (
            <ul className={styles.biasList}>
              {topBiases.map((s) => (
                <li key={s.category}>
                  <span>{s.category.replaceAll("_", " ")}</span>
                  <span>{s.ema.toFixed(2)}</span>
                </li>
              ))}
            </ul>
          )}
        </aside>
      )}

      <section className={styles.ask}>
        <h1 className={styles.title}>Bring a real decision</h1>
        <p className={styles.sub}>
          Level will ask the hard clarifying question — with citations from your sources.
        </p>
        {turns.map((turn) => (
          <article key={turn.turn_id} className={styles.turn}>
            {turn.user_text && <p className={styles.you}>{turn.user_text}</p>}
            {turn.challenger_questions.map((q, i) => (
              <div key={i} className={styles.level}>
                <p>{q.question}</p>
                {q.citations.length > 0 && (
                  <ul className={styles.cites}>
                    {q.citations.map((c) => (
                      <li key={c.fact_id}>{c.quote}</li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </article>
        ))}
        <form onSubmit={onSubmit} className={styles.form}>
          {error && <p className={styles.error}>{error}</p>}
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={4}
            placeholder="What’s the decision you’re sitting with?"
            disabled={busy || !userId}
          />
          <button type="submit" disabled={busy || !draft.trim() || !userId}>
            {busy ? "Thinking…" : "Ask Level"}
          </button>
        </form>
      </section>
    </AppShell>
  );
}

export default function SessionPage() {
  return <SessionPageInner />;
}
