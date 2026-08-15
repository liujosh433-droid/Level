"use client";

import { CSSProperties, Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ActivityIcon } from "@/components/ActivityIcon";
import { AppShell } from "@/components/AppShell";
import {
  DashboardWorkspace,
  CareLoadGraph,
  RailSection,
  RoleLoadBar,
  TellLevelPanel,
  TellLevelReply,
  TellLevelYou,
} from "@/components/dashboard";
import {
  AuthError,
  confirmProposal,
  dayCheckIn,
  declineProposal,
  fetchMe,
  fetchToday,
  proposeSchedule,
  readTodayCache,
  resolveUsual,
  submitSchoolPaper,
  writeTodayCache,
  type CommitmentProposal,
  type TodayView,
  type Turn,
} from "@/lib/api";
import styles from "./today.module.css";

type ChatItem =
  | { id: string; kind: "turn"; turn: Turn }
  | { id: string; kind: "proposal"; proposal: CommitmentProposal }
  | { id: string; kind: "checkin"; you: string; reply: string };

function buildDayScript(view: TodayView): string {
  const name = view.greeting_name || "there";
  const parts: string[] = [
    `Hi ${name}. Here's your Level briefing for ${view.weekday_label}.`,
  ];
  if (view.events.length === 0) {
    parts.push("Your calendar looks clear today.");
  } else {
    parts.push(
      `You have ${view.events.length} thing${view.events.length === 1 ? "" : "s"} on the calendar.`,
    );
    for (const ev of view.events.slice(0, 8)) {
      const when = ev.when_label || "Sometime today";
      // "At …," + period gives the synthesizer a clearer event boundary than "8:00 AM: …"
      let line = `At ${when}, ${ev.summary}.`;
      if (ev.cues?.length) {
        line += ` Remember: ${ev.cues.join(". ")}.`;
      }
      parts.push(line);
    }
  }
  if (view.recommendations?.length) {
    parts.push(
      `From what we know about you: ${view.recommendations.slice(0, 2).join(" ")}`,
    );
  }
  parts.push("You've got this — one honest step at a time.");
  // Ellipsis between beats → a short spoken pause (space alone smushes events together).
  return parts.join(" ... ");
}

function TodayInner() {
  const router = useRouter();
  const [userId, setUserId] = useState("");
  const [displayName, setDisplayName] = useState<string | null>(null);
  const [today, setToday] = useState<TodayView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [paperText, setPaperText] = useState("");
  const [paperFile, setPaperFile] = useState<File | null>(null);
  const [paperFileKey, setPaperFileKey] = useState(0);
  const [busy, setBusy] = useState(false);
  const [canSendEmail, setCanSendEmail] = useState(true);
  const [speaking, setSpeaking] = useState(false);
  const [items, setItems] = useState<ChatItem[]>([]);
  const [booting, setBooting] = useState(true);
  const speakGenRef = useRef(0);
  const speakTimerRef = useRef<number | null>(null);

  useEffect(() => {
    const cached = readTodayCache();
    if (cached) {
      setToday(cached);
      if (cached.display_name) setDisplayName(cached.display_name);
      setBooting(false);
    }
    void (async () => {
      try {
        const [me, data] = await Promise.all([fetchMe(), fetchToday()]);
        setUserId(me.user_id);
        setDisplayName(me.display_name);
        setCanSendEmail(me.can_send_email !== false);
        if (!me.google_connected && !data.google_connected) {
          router.replace("/sources");
          return;
        }
        setToday(data);
        writeTodayCache(data);
        if (data.display_name) setDisplayName(data.display_name);
      } catch (err) {
        if (err instanceof AuthError) {
          router.replace("/welcome");
          return;
        }
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBooting(false);
      }
    })();
  }, [router]);

  function refreshToday() {
    return fetchToday()
      .then((data) => {
        setToday(data);
        writeTodayCache(data);
        if (data.display_name) setDisplayName(data.display_name);
      })
      .catch(() => undefined);
  }

  // Care graph builds in the background after Google connect — soft-poll a few times.
  useEffect(() => {
    if (!today?.google_connected) return;
    if (today.care_graph && today.care_graph.nodes.length > 0) return;
    let cancelled = false;
    const delays = [2500, 6000, 12000];
    const timers = delays.map((ms) =>
      window.setTimeout(() => {
        if (!cancelled) void refreshToday();
      }, ms),
    );
    return () => {
      cancelled = true;
      for (const t of timers) window.clearTimeout(t);
    };
  }, [today?.google_connected, today?.care_graph?.nodes?.length]);

  function clearSpeakTimer() {
    if (speakTimerRef.current != null) {
      window.clearTimeout(speakTimerRef.current);
      speakTimerRef.current = null;
    }
  }

  function stopSpeaking() {
    speakGenRef.current += 1;
    clearSpeakTimer();
    if (typeof window !== "undefined" && window.speechSynthesis) {
      window.speechSynthesis.onvoiceschanged = null;
      // Chrome sometimes ignores cancel while "paused"; resume first.
      try {
        window.speechSynthesis.resume();
      } catch {
        /* ignore */
      }
      window.speechSynthesis.cancel();
    }
    setSpeaking(false);
  }

  function speakDay() {
    if (!today) return;
    if (typeof window === "undefined" || !window.speechSynthesis) {
      setError("Spoken briefing isn’t supported in this browser — try Chrome or Safari.");
      return;
    }
    if (speaking) {
      stopSpeaking();
      return;
    }
    const script = buildDayScript(today);
    const gen = ++speakGenRef.current;

    const start = () => {
      if (gen !== speakGenRef.current) return;
      const utter = new SpeechSynthesisUtterance(script);
      utter.rate = 1.02;
      utter.pitch = 1;
      const voices = window.speechSynthesis.getVoices();
      const preferred =
        voices.find(
          (v) => /en(-|_)US/i.test(v.lang) && /natural|enhanced|premium/i.test(v.name),
        ) ||
        voices.find((v) => /en(-|_)US/i.test(v.lang)) ||
        voices.find((v) => v.lang.startsWith("en"));
      if (preferred) utter.voice = preferred;
      utter.onend = () => {
        if (gen === speakGenRef.current) setSpeaking(false);
      };
      utter.onerror = () => {
        if (gen === speakGenRef.current) setSpeaking(false);
      };
      setSpeaking(true);
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(utter);
    };

    // Chrome often loads voices asynchronously.
    if (window.speechSynthesis.getVoices().length === 0) {
      window.speechSynthesis.onvoiceschanged = () => {
        window.speechSynthesis.onvoiceschanged = null;
        start();
      };
      clearSpeakTimer();
      speakTimerRef.current = window.setTimeout(start, 250);
      return;
    }
    start();
  }

  useEffect(() => {
    return () => {
      speakGenRef.current += 1;
      clearSpeakTimer();
      if (typeof window !== "undefined" && window.speechSynthesis) {
        window.speechSynthesis.cancel();
      }
    };
  }, []);

  async function onAsk(text: string) {
    if (!userId || !text.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      // AI decides schedule vs not — no client keyword gate.
      const schedule = await proposeSchedule(text);
      if (schedule.is_schedule_ask && schedule.proposal) {
        setItems((prev) => [
          ...prev,
          { id: schedule.proposal!.proposal_id, kind: "proposal", proposal: schedule.proposal! },
        ]);
        setDraft("");
        return;
      }

      // Day reflections / tips → profile + event cues
      const check = await dayCheckIn(text);
      const school = check.school_proposals ?? [];
      if (school.length > 0) {
        setItems((prev) => [
          ...prev,
          ...school.map((proposal) => ({
            id: proposal.proposal_id,
            kind: "proposal" as const,
            proposal,
          })),
        ]);
      } else {
        setItems((prev) => [
          ...prev,
          {
            id: `checkin-${Date.now()}`,
            kind: "checkin",
            you: text,
            reply: check.reply,
          },
        ]);
      }
      setDraft("");
      void refreshToday();
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
      await refreshToday();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function onUsual(
    usualId: string,
    action: "put_back" | "exception" | "not_me" | "keep",
    onDate?: string,
  ) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await resolveUsual(usualId, action, onDate);
      await refreshToday();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function onSchoolPaper() {
    const text = paperText.trim();
    if ((!text && !paperFile) || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await submitSchoolPaper(text, paperFile);
      if (res.proposal) {
        setItems((prev) => [
          ...prev,
          { id: res.proposal!.proposal_id, kind: "proposal", proposal: res.proposal! },
        ]);
        setPaperText("");
        setPaperFile(null);
        setPaperFileKey((k) => k + 1);
      } else if (res.ask) {
        setItems((prev) => [
          ...prev,
          {
            id: `paper-${Date.now()}`,
            kind: "checkin",
            you: text.slice(0, 160),
            reply: res.ask ?? "",
          },
        ]);
      }
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

  const name = today?.greeting_name?.trim() || null;
  const weekday = today?.weekday_label?.trim() || null;

  const hearDayButton = today ? (
    <button
      type="button"
      className={speaking ? styles.speakOn : styles.speak}
      onClick={speakDay}
      aria-pressed={speaking}
      aria-label={speaking ? "Stop hearing your day" : "Hear Level describe your day"}
    >
      <svg
        className={styles.speakIcon}
        viewBox="0 0 24 24"
        width="16"
        height="16"
        aria-hidden="true"
      >
        {speaking ? (
          <path fill="currentColor" d="M6.5 6.5h11v11h-11z" />
        ) : (
          <>
            <path
              fill="currentColor"
              d="M3 9v6h4l5 4V5L7 9H3zm13.5 3a4.5 4.5 0 0 0-2.3-3.9v7.8A4.4 4.4 0 0 0 16.5 12z"
            />
            <path
              fill="currentColor"
              d="M16.2 4.1v2.1a6.9 6.9 0 0 1 0 11.6v2.1a9 9 0 0 0 0-15.8z"
            />
          </>
        )}
      </svg>
      {speaking ? "Stop" : "Hear my day"}
    </button>
  ) : null;

  const chatThread = items.map((item) =>
    item.kind === "turn" ? (
      <article key={item.id} className={styles.turn}>
        {item.turn.user_text && <TellLevelYou>{item.turn.user_text}</TellLevelYou>}
        {item.turn.challenger_questions.map((q, i) => (
          <TellLevelReply key={i}>
            <p>{q.question}</p>
            {q.citations.length > 0 && (
              <ul className={styles.cites}>
                {q.citations.map((c) => (
                  <li key={c.fact_id}>{c.quote}</li>
                ))}
              </ul>
            )}
          </TellLevelReply>
        ))}
        {(item.turn.status === "degraded" || item.turn.status === "blocked") && (
          <TellLevelReply>
            <p className={styles.admit}>
              {item.turn.status === "blocked"
                ? "I blocked that reply — it didn’t pass Level’s safety check. Try rephrasing, or ask again in a moment."
                : "I couldn’t finish a grounded challenge this turn (retrieval or the model glitched). Your care roles are still saved — try again."}
            </p>
            {item.turn.degradation_reason ? (
              <p className={styles.admitDetail}>{item.turn.degradation_reason}</p>
            ) : null}
          </TellLevelReply>
        )}
      </article>
    ) : item.kind === "checkin" ? (
      <article key={item.id} className={styles.turn}>
        <TellLevelYou>{item.you}</TellLevelYou>
        <TellLevelReply>{item.reply}</TellLevelReply>
      </article>
    ) : (
      <article key={item.id} className={styles.turn}>
        <TellLevelYou>{item.proposal.user_text}</TellLevelYou>
        <TellLevelReply>
          <p className={styles.proposalSummary}>{item.proposal.summary}</p>
          <p>{item.proposal.level_message}</p>
          {item.proposal.kind === "school_send" && item.proposal.to_email ? (
            <div className={styles.emailPreview}>
              <p>
                <strong>To:</strong> {item.proposal.to_email}
              </p>
              {item.proposal.email_subject ? (
                <p>
                  <strong>Subject:</strong> {item.proposal.email_subject}
                </p>
              ) : null}
              {item.proposal.email_body ? (
                <pre>{item.proposal.email_body}</pre>
              ) : null}
            </div>
          ) : null}
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
              {item.proposal.kind === "add" &&
                item.proposal.recommended_action === "confirm" && (
                <button
                  type="button"
                  className={styles.primaryAction}
                  disabled={busy}
                  onClick={() => void onConfirm(item.proposal)}
                >
                  Add anyway
                </button>
              )}
              {item.proposal.kind === "school_send" &&
                item.proposal.status === "pending" &&
                (canSendEmail ? (
                <button
                  type="button"
                  className={styles.primaryAction}
                  disabled={busy}
                  onClick={() => void onConfirm(item.proposal)}
                >
                  Send
                </button>
                ) : (
                <a href="/sources?need_gmail=1" className={styles.primaryAction}>
                  Allow sending email on Sources
                </a>
                ))}
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
            <p className={styles.meta}>
              {item.proposal.kind === "school_send"
                ? "Sent — the school has it."
                : "Added to your Google Calendar."}
            </p>
          )}
          {item.proposal.status === "declined" && (
            <p className={styles.meta}>Okay — left off the calendar.</p>
          )}
        </TellLevelReply>
      </article>
    ),
  );

  return (
    <AppShell userId={userId} displayName={displayName} dashboard contentOnly>
      <DashboardWorkspace
        railAriaLabel="Reminders and ask Level"
        rail={
          <>
            <RailSection title="Reminders">
              {booting || !today ? (
                <p className={styles.meta}>Loading reminders…</p>
              ) : today.recommendations.length > 0 ? (
                <ul className={styles.reminders}>
                  {today.recommendations.map((r) => (
                    <li key={r}>{r}</li>
                  ))}
                </ul>
              ) : (
                <p className={styles.meta}>
                  Nothing specific for today’s events yet. Tell Level something once (like forgetting
                  soccer shoes) and it’ll remind you when that day comes up.
                </p>
              )}
            </RailSection>

            <div className={styles.railStack}>
              <div className={styles.chatBlock}>
                <TellLevelPanel
                  title="Ask Level"
                  lead="How’s the day going - or facing a hard call? Level can weigh it with you using your calendar, learned priorities, and interactions. It can also
                  book events for you!"
                  placeholder="How’s the day going — or is someone home sick?"
                  value={draft}
                  onChange={setDraft}
                  onSubmit={onAsk}
                  busy={busy}
                  busyLabel="Thinking…"
                  busyHints={[
                    "Looking at your calendar…",
                    "Weighing decisions and priorities…",
                    "Checking your care load…",
                    "Putting an honest answer together…",
                  ]}
                  disabled={!userId || booting}
                  voiceEnabled
                  stickyInput={false}
                  onVoiceError={setError}
                  error={error}
                  headerActions={hearDayButton}
                >
                  {items.length > 0 ? chatThread : null}
                </TellLevelPanel>
              </div>
              <RailSection title="School notes">
                <p className={styles.meta}>
                  Upload or paste a slip — Level holds the deadline and drafts the school email.
                  Or tell Level someone is home sick for the attendance note. You Run the send.
                </p>
                <label className={styles.paperFile}>
                  <input
                    key={paperFileKey}
                    type="file"
                    accept="application/pdf,image/*,.txt"
                    disabled={busy || !userId}
                    onChange={(e) => setPaperFile(e.target.files?.[0] ?? null)}
                  />
                  <span>{paperFile ? paperFile.name : "Upload a PDF or photo"}</span>
                </label>
                <textarea
                  className={styles.paperInput}
                  rows={3}
                  value={paperText}
                  onChange={(e) => setPaperText(e.target.value)}
                  placeholder="Or paste the form text…"
                  disabled={busy || !userId}
                />
                <button
                  type="button"
                  className={styles.secondaryAction}
                  disabled={busy || (!paperText.trim() && !paperFile)}
                  onClick={() => void onSchoolPaper()}
                >
                  Hold the deadline
                </button>
              </RailSection>
              <div className={styles.graphBlock}>
                <CareLoadGraph
                  graph={today?.care_graph}
                  building={Boolean(
                    today?.google_connected &&
                      (!today.care_graph || today.care_graph.nodes.length === 0),
                  )}
                />
              </div>
            </div>
          </>
        }
      >
        <div className={styles.titleRow}>
          <div className={styles.titleBlock}>
            {booting || !today ? (
              <p className={styles.bootMsg}>Loading your day…</p>
            ) : (
              <>
                {today.date_label ? (
                  <p className={styles.dateLabel}>{today.date_label}</p>
                ) : null}
                <h1 className={styles.title}>
                  Hi {name || "there"}
                  {weekday ? `, Happy ${weekday}` : ""}!
                </h1>
                {today.holding && today.holding.length > 0 ? (
                  <p className={styles.holdingLine}>
                    <span className={styles.holdingLabel}>Today&rsquo;s care</span>
                    <span className={styles.holdingChips}>
                      {today.holding.map((h, i) => (
                        <span key={`${h.role_id}-${h.label}`}>
                          {i > 0 ? (
                            <span className={styles.holdingSep} aria-hidden>
                              ·
                            </span>
                          ) : null}
                          <span
                            className={styles.holdingChip}
                            style={{ ["--hold-color" as string]: h.color }}
                          >
                            {h.label}
                          </span>
                        </span>
                      ))}
                    </span>
                  </p>
                ) : null}
                <RoleLoadBar load={today.week_load} />
              </>
            )}
          </div>
        </div>

        {today?.proposed_usuals && today.proposed_usuals.length > 0 && (
          <div className={styles.bannerStack}>
            {today.proposed_usuals.map((usual) => (
              <div key={usual.usual_id} className={styles.banner}>
                <p>
                  <strong>Usual?</strong> {usual.label}
                  {usual.display_name ? ` for ${usual.display_name}` : ""}
                  {usual.your_role ? ` — you’re the ${usual.your_role}` : ""}.
                </p>
                <div className={styles.bannerActions}>
                  <button
                    type="button"
                    className={styles.primaryAction}
                    disabled={busy}
                    onClick={() => void onUsual(usual.usual_id, "keep")}
                  >
                    Keep
                  </button>
                  <button
                    type="button"
                    className={styles.ghostAction}
                    disabled={busy}
                    onClick={() => void onUsual(usual.usual_id, "not_me")}
                  >
                    Not me
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}

        {today?.usual_gaps && today.usual_gaps.length > 0 && (
          <div className={styles.bannerStack}>
            {today.usual_gaps.map((gap) => (
              <div key={`${gap.usual_id}-${gap.on_date}`} className={styles.banner}>
                <p>
                  <strong>Missing usual:</strong> {gap.banner}
                </p>
                <div className={styles.bannerActions}>
                  <button
                    type="button"
                    className={styles.primaryAction}
                    disabled={busy}
                    onClick={() => void onUsual(gap.usual_id, "put_back", gap.on_date)}
                  >
                    Put it back
                  </button>
                  <button
                    type="button"
                    className={styles.secondaryAction}
                    disabled={busy}
                    onClick={() => void onUsual(gap.usual_id, "exception", gap.on_date)}
                  >
                    This week is different
                  </button>
                  <button
                    type="button"
                    className={styles.ghostAction}
                    disabled={busy}
                    onClick={() => void onUsual(gap.usual_id, "not_me", gap.on_date)}
                  >
                    Not me
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}

        {today?.pending_challenges && today.pending_challenges.length > 0 && (
          <div className={styles.banner}>
            <p>
              <strong>Care collision:</strong>{" "}
              {today.pending_challenges[0].trigger_label}
            </p>
            {today.pending_challenges[0].question ? (
              <p style={{ marginTop: "0.4rem" }}>{today.pending_challenges[0].question}</p>
            ) : null}
            <button
              type="button"
              className={styles.linkish}
              style={{ marginTop: "0.5rem" }}
              onClick={() => {
                const ch = today.pending_challenges![0];
                if (ch.question) {
                  setItems((prev) => [
                    {
                      id: `pending-${ch.decision_id}`,
                      kind: "turn",
                      turn: {
                        turn_id: `pending-${ch.decision_id}`,
                        status: "complete",
                        challenger_questions: [
                          {
                            question: ch.question!,
                            challenge_type: ch.challenge_type || "role_theft",
                            citations: [],
                          },
                        ],
                      },
                    },
                    ...prev.filter((i) => i.id !== `pending-${ch.decision_id}`),
                  ]);
                }
              }}
            >
              Open challenge →
            </button>
          </div>
        )}

        {today?.needs_review && (
          <p className={styles.banner}>
            Level drafted your care roles — take 30 seconds to confirm what you hold.{" "}
            <Link href="/profile">Open About me →</Link>
          </p>
        )}

        <section className={styles.block}>
          <h2>On your calendar</h2>
          {booting || !today ? (
            <p className={styles.meta}>Loading calendar…</p>
          ) : today.events.length === 0 ? (
            <p className={styles.meta}>Nothing on the calendar for today.</p>
          ) : (
            <ul className={styles.events}>
              {today.events.map((ev) => {
                const color = ev.color || "#8aa4b0";
                return (
                  <li
                    key={ev.id || `${ev.summary}-${ev.start}`}
                    className={styles.eventCard}
                    data-kind={ev.activity_kind}
                    style={{ ["--event-color" as string]: color } as CSSProperties}
                  >
                    <div className={styles.eventArt} aria-hidden>
                      <ActivityIcon kind={ev.activity_kind} className={styles.eventIcon} />
                    </div>
                    <div className={styles.eventBody}>
                      <div className={styles.eventMeta}>
                        <span className={styles.when}>{ev.when_label}</span>
                        <span className={styles.kindChip}>{ev.activity_kind}</span>
                      </div>
                      <span className={styles.eventTitle}>{ev.summary}</span>
                      {ev.cues?.length > 0 && (
                        <ul className={styles.eventCues}>
                          {ev.cues.map((c) => (
                            <li key={c}>{c}</li>
                          ))}
                        </ul>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        {today?.tomorrow && (
          <section className={`${styles.block} ${styles.tomorrow}`}>
            <div className={styles.tomorrowHead}>
              <div>
                <h2>Tomorrow</h2>
                <p className={styles.tomorrowDate}>
                  {today.tomorrow.weekday_label}
                  {today.tomorrow.date_label ? ` · ${today.tomorrow.date_label}` : ""}
                </p>
              </div>
              <span className={styles.countChip}>
                {today.tomorrow.events.length === 0
                  ? "Clear"
                  : `${today.tomorrow.events.length} event${
                      today.tomorrow.events.length === 1 ? "" : "s"
                    }`}
              </span>
            </div>

            {today.tomorrow.events.length === 0 ? (
              <p className={styles.meta}>Nothing on the calendar yet.</p>
            ) : (
              <ul className={styles.tomorrowEvents}>
                {today.tomorrow.events.map((ev) => (
                  <li
                    key={ev.id || `t-${ev.summary}-${ev.start}`}
                    style={{ ["--event-color" as string]: ev.color || "#8aa4b0" }}
                  >
                    <span className={styles.tomDot} aria-hidden="true" />
                    <span className={styles.when}>{ev.when_label}</span>
                    <div className={styles.tomBody}>
                      <span className={styles.tomTitle}>{ev.summary}</span>
                      {ev.cues?.[0] ? (
                        <span className={styles.tomCue}>{ev.cues[0]}</span>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ul>
            )}

            {today.tomorrow.remember.length > 0 && (
              <div className={styles.tipRow}>
                <p className={styles.tipLabel}>Remember</p>
                <ul className={styles.tipChips}>
                  {today.tomorrow.remember.map((r) => (
                    <li key={r}>{r}</li>
                  ))}
                </ul>
              </div>
            )}
          </section>
        )}
      </DashboardWorkspace>
    </AppShell>
  );
}

export default function TodayPage() {
  return (
    <Suspense fallback={<AppShell contentOnly><p className={styles.meta}>Loading…</p></AppShell>}>
      <TodayInner />
    </Suspense>
  );
}
