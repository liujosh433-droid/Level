"use client";

import { FormEvent, Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import {
  AuthError,
  confirmProposal,
  createDecision,
  declineProposal,
  fetchMe,
  fetchToday,
  proposeSchedule,
  takeTurn,
  type CommitmentProposal,
  type TodayView,
  type Turn,
} from "@/lib/api";
import styles from "./today.module.css";

type ChatItem =
  | { id: string; kind: "turn"; turn: Turn }
  | { id: string; kind: "proposal"; proposal: CommitmentProposal };

function TodayInner() {
  const router = useRouter();
  const [userId, setUserId] = useState("");
  const [today, setToday] = useState<TodayView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [listening, setListening] = useState(false);
  const [decisionId, setDecisionId] = useState<string | null>(null);
  const [items, setItems] = useState<ChatItem[]>([]);
  const recognitionRef = useRef<SpeechRecognition | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const me = await fetchMe();
        setUserId(me.user_id);
        if (!me.google_connected) {
          router.replace("/sources");
          return;
        }
        const data = await fetchToday();
        setToday(data);
      } catch (err) {
        if (err instanceof AuthError) {
          router.replace("/welcome");
          return;
        }
        setError(err instanceof Error ? err.message : String(err));
      }
    })();
  }, [router]);

  function refreshToday() {
    void fetchToday()
      .then(setToday)
      .catch(() => undefined);
  }

  function toggleVoice() {
    const SR =
      typeof window !== "undefined"
        ? window.SpeechRecognition || window.webkitSpeechRecognition
        : undefined;
    if (!SR) {
      setError("Voice isn’t supported in this browser — try Chrome, or type instead.");
      return;
    }
    if (listening && recognitionRef.current) {
      recognitionRef.current.stop();
      setListening(false);
      return;
    }
    const rec = new SR();
    recognitionRef.current = rec;
    rec.lang = "en-US";
    rec.interimResults = false;
    rec.onresult = (ev: SpeechRecognitionEvent) => {
      const text = Array.from(ev.results)
        .map((r) => r[0]?.transcript ?? "")
        .join(" ")
        .trim();
      if (text) setDraft((prev) => (prev ? `${prev.trim()} ${text}` : text));
    };
    rec.onerror = () => setListening(false);
    rec.onend = () => setListening(false);
    setListening(true);
    rec.start();
  }

  async function onAsk(e: FormEvent) {
    e.preventDefault();
    if (!userId || !draft.trim() || busy) return;
    setBusy(true);
    setError(null);
    const text = draft.trim();
    try {
      const schedule = await proposeSchedule(text);
      if (schedule.is_schedule_ask && schedule.proposal) {
        setItems((prev) => [
          ...prev,
          { id: schedule.proposal!.proposal_id, kind: "proposal", proposal: schedule.proposal! },
        ]);
        setDraft("");
        return;
      }

      let id = decisionId;
      if (!id) {
        const d = await createDecision();
        id = d.decision_id;
        setDecisionId(id);
      }
      const turn = await takeTurn(id, text);
      setItems((prev) => [...prev, { id: turn.turn_id, kind: "turn", turn }]);
      setDraft("");
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

  async function onConfirm(proposal: CommitmentProposal, slotStart?: string) {
    if (!userId || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await confirmProposal(proposal.proposal_id, slotStart);
      setItems((prev) =>
        prev.map((it) =>
          it.kind === "proposal" && it.proposal.proposal_id === proposal.proposal_id
            ? { ...it, proposal: res.proposal }
            : it,
        ),
      );
      refreshToday();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function onDecline(proposal: CommitmentProposal) {
    if (!userId || busy) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await declineProposal(proposal.proposal_id);
      setItems((prev) =>
        prev.map((it) =>
          it.kind === "proposal" && it.proposal.proposal_id === proposal.proposal_id
            ? { ...it, proposal: updated }
            : it,
        ),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AppShell userId={userId}>
      <p className={styles.kicker}>Today</p>
      <h1 className={styles.title}>Your day</h1>

      {today?.needs_review && (
        <p className={styles.banner}>
          Level drafted your profile — take 30 seconds to confirm it.{" "}
          <Link href="/profile">Review profile →</Link>
        </p>
      )}

      <section className={styles.block}>
        <h2>On your calendar</h2>
        {!today ? (
          <p className={styles.meta}>Loading…</p>
        ) : today.events.length === 0 ? (
          <p className={styles.meta}>Nothing on the calendar for today.</p>
        ) : (
          <ul className={styles.events}>
            {today.events.map((ev) => (
              <li key={ev.id || `${ev.summary}-${ev.start}`}>
                <span className={styles.when}>{ev.when_label}</span>
                <span>{ev.summary}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className={styles.block}>
        <h2>Level recommends</h2>
        {today && today.recommendations.length > 0 ? (
          <ul className={styles.recs}>
            {today.recommendations.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        ) : (
          <p className={styles.meta}>
            Sync your calendar on Sources to get day-specific nudges.
          </p>
        )}
      </section>

      <section className={styles.block}>
        <h2>Ask Level</h2>
        <p className={styles.meta}>
          Decisions, “do I have time?”, or “add this to my calendar” — Level checks
          your schedule and profile before you commit.
        </p>
        {items.map((item) =>
          item.kind === "turn" ? (
            <article key={item.id} className={styles.turn}>
              {item.turn.user_text && <p className={styles.you}>{item.turn.user_text}</p>}
              {item.turn.challenger_questions.map((q, i) => (
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
          ) : (
            <article key={item.id} className={styles.turn}>
              <p className={styles.you}>{item.proposal.user_text}</p>
              <div className={styles.level}>
                <p className={styles.proposalSummary}>{item.proposal.summary}</p>
                <p>{item.proposal.level_message}</p>
                {item.proposal.conflicts.length > 0 && (
                  <ul className={styles.cites}>
                    {item.proposal.conflicts.map((c) => (
                      <li key={`${c.summary}-${c.start}`}>Conflict: {c.label}</li>
                    ))}
                  </ul>
                )}
                {item.proposal.citations.length > 0 && (
                  <ul className={styles.cites}>
                    {item.proposal.citations.map((c) => (
                      <li key={c.fact_id}>{c.quote}</li>
                    ))}
                  </ul>
                )}
                {item.proposal.status === "pending" && (
                  <div className={styles.proposalActions}>
                    {(item.proposal.kind === "add" ||
                      item.proposal.recommended_action === "confirm") && (
                      <button
                        type="button"
                        className={styles.primaryAction}
                        disabled={busy}
                        onClick={() => void onConfirm(item.proposal)}
                      >
                        {item.proposal.kind === "availability"
                          ? "Book this time"
                          : "Add anyway"}
                      </button>
                    )}
                    {item.proposal.free_slots.slice(0, 3).map((slot) => (
                      <button
                        key={slot.start}
                        type="button"
                        className={styles.secondaryAction}
                        disabled={busy}
                        onClick={() => void onConfirm(item.proposal, slot.start)}
                      >
                        Use {slot.label}
                      </button>
                    ))}
                    <button
                      type="button"
                      className={styles.ghostAction}
                      disabled={busy}
                      onClick={() => void onDecline(item.proposal)}
                    >
                      Never mind
                    </button>
                  </div>
                )}
                {item.proposal.status === "confirmed" && (
                  <p className={styles.meta}>Added to your Google Calendar.</p>
                )}
                {item.proposal.status === "declined" && (
                  <p className={styles.meta}>Okay — left off the calendar.</p>
                )}
              </div>
            </article>
          ),
        )}
        <div className={styles.askDock}>
          <form onSubmit={onAsk} className={styles.ask}>
            {error && <p className={styles.error}>{error}</p>}
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={3}
              placeholder='Try: “Diane wants dinner at 6:30 tonight — do I have time?”'
              disabled={busy || !userId}
            />
            <div className={styles.askRow}>
              <button
                type="button"
                className={styles.voiceBtn}
                onClick={toggleVoice}
                disabled={busy || !userId}
                aria-pressed={listening}
              >
                {listening ? "Stop" : "Voice"}
              </button>
              <button type="submit" disabled={busy || !draft.trim()}>
                {busy ? "Listening…" : "Ask"}
              </button>
            </div>
          </form>
        </div>
      </section>
    </AppShell>
  );
}

export default function TodayPage() {
  return (
    <Suspense fallback={<AppShell><p className={styles.meta}>Loading…</p></AppShell>}>
      <TodayInner />
    </Suspense>
  );
}
