"use client";

import {
  FormEvent,
  ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { api } from "@/lib/api";
import { listenOnce, speak, voiceSupported } from "@/lib/voice";
import styles from "./Chat.module.css";

type EmailDraft = {
  to: string;
  subject: string;
  body: string;
  confirmation_token: string;
  contact_name: string;
  person_name?: string | null;
  kind?: string | null;
};

type FeedbackVerdict = "keep" | "adjust" | "not_me";

type Message = {
  id: string;
  role: "user" | "assistant";
  text: string;
  emailDraft?: EmailDraft;
  emailSent?: boolean;
  path?: string;
  intent?: string;
  needsClarify?: boolean;
  // Priority texts this reply surfaced. Rendered in teal within the
  // bubble so caregivers can see at a glance which stated priorities
  // Level thinks are relevant to what they just asked.
  priorityHits?: string[];
  streaming?: boolean;
  feedback?: {
    agent: string;
    field: string;
    value: string;
    verdict?: FeedbackVerdict;
    submitting?: FeedbackVerdict;
  };
};

type Props = {
  title?: string;
  lead?: string;
  placeholder?: string;
  headerActions?: ReactNode;
  onAfterReply?: () => void;
  onSpeakDay?: () => void;
  busyHints?: string[];
};

const DEFAULT_BUSY_HINTS = [
  "Thinking\u2026",
  "One moment\u2026",
  "Working on it\u2026",
  "Almost there\u2026",
];

// Intent-specific busy hints. Email drafting hits Gemini Pro with a
// tone/template pass so it takes 3-8s consistently; a specific
// "Drafting the email\u2026" bubble makes the wait feel intentional
// instead of like silent stalling. Booking also touches an LLM +
// Google Calendar write when the deterministic parser misses.
const EMAIL_DRAFTING_HINTS = [
  "Drafting the email\u2026",
  "Choosing the right tone\u2026",
  "Polishing the wording\u2026",
];
const BOOKING_HINTS = [
  "Finding a good time\u2026",
  "Checking your calendar\u2026",
  "Locking the slot in\u2026",
];

// Client-side intent sniffers mirror the server's fast-path regexes
// (see `level_core/email/resolve.py`::is_email_request). We're only
// picking a status label - if we guess wrong the request still goes
// through the router, the user just sees a mildly off hint.
const _EMAIL_WORD = /\be-?mails?\b/i;
const _SICK_NOTE_HINT =
  /\b(?:sick\s+notes?|absence\s+notes?|excuse(?:d)?\s+(?:notes?|absence)|school\s+notes?)\b/i;
const _TELL_ROLE_HINT =
  /(?:tell|let)\s+(?:her|him|them|their)\s+(?:teacher|doctor|coach|dr\.?)\s+know/i;
const _CONTACT_ROLE_HINT =
  /\b(?:teachers?|homeroom|doctors?|dr\.?|pediatrician|coaches?|coach)\b/i;
const _SENDISH_HINT = /\b(?:send|write|draft|email|message|text)\b/i;
const _NOTE_WORD_HINT = /\b(?:notes?|messages?)\b/i;

function looksLikeEmailRequest(text: string): boolean {
  if (_EMAIL_WORD.test(text) || _SICK_NOTE_HINT.test(text) || _TELL_ROLE_HINT.test(text)) {
    return true;
  }
  if (_CONTACT_ROLE_HINT.test(text) && (_SENDISH_HINT.test(text) || _NOTE_WORD_HINT.test(text))) {
    return true;
  }
  if (_SENDISH_HINT.test(text) && _NOTE_WORD_HINT.test(text)) {
    return true;
  }
  return false;
}

// Booking-shaped verbs. This is intentionally NARROW - most calendar
// asks hit the deterministic fast path (<100ms) and don't need a
// reassurance bubble. We only want the specific "book/schedule X on
// {day} at {time}" style that runs BookAgent.
const _BOOKING_HINT = /\b(?:book|schedule|reschedule|move|cancel|find\s+(?:a\s+)?(?:good\s+)?time|when\s+can)\b/i;

function looksLikeBookingRequest(text: string): boolean {
  return _BOOKING_HINT.test(text);
}

function hintsForMessage(text: string, fallback: string[]): string[] {
  if (looksLikeEmailRequest(text)) return EMAIL_DRAFTING_HINTS;
  if (looksLikeBookingRequest(text)) return BOOKING_HINTS;
  return fallback;
}

// Regex-quote a string so it can be dropped into `new RegExp(...)`
// without accidentally activating metacharacters. Priority text is
// user-controlled ("elder care with mom takes precedent (over work)")
// so parens/brackets/etc. are realistic.
function escapeForRegex(input: string): string {
  return input.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Render a reply where any of the given priority strings should be
// highlighted in teal. We match case-insensitively but preserve the
// original casing that appears in the reply. Priorities can appear
// inside curly quotes (\u201c...\u201d) so we strip those before match.
function renderReplyWithHighlights(
  text: string,
  highlights: readonly string[] | undefined,
): ReactNode {
  if (!highlights || highlights.length === 0) return text;
  // Sort by length descending so longer priorities match before their
  // substrings (e.g. "elder care with mom" before "elder care").
  const patterns = [...highlights]
    .filter((h) => h && h.trim().length > 0)
    .sort((a, b) => b.length - a.length)
    .map(escapeForRegex);
  if (patterns.length === 0) return text;
  const combined = new RegExp(`(${patterns.join("|")})`, "gi");
  const chunks = text.split(combined);
  return chunks.map((chunk, i) => {
    if (i % 2 === 1) {
      return (
        <span key={`p-${i}`} className={styles.priorityHit}>
          {chunk}
        </span>
      );
    }
    return chunk;
  });
}

const MicIcon = ({ className }: { className?: string }) => (
  <svg
    className={className}
    viewBox="0 0 24 24"
    width="16"
    height="16"
    aria-hidden="true"
  >
    <path
      fill="currentColor"
      d="M12 14a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v5a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z"
    />
  </svg>
);

const SpeakerIcon = ({ className }: { className?: string }) => (
  <svg
    className={className}
    viewBox="0 0 24 24"
    width="16"
    height="16"
    aria-hidden="true"
  >
    <path
      fill="currentColor"
      d="M3 9v6h4l5 4V5L7 9H3zm13.5 3a4.5 4.5 0 0 0-2.3-3.9v7.8A4.4 4.4 0 0 0 16.5 12z"
    />
    <path
      fill="currentColor"
      d="M16.2 4.1v2.1a6.9 6.9 0 0 1 0 11.6v2.1a9 9 0 0 0 0-15.8z"
    />
  </svg>
);

let messageSeq = 0;

function nextId(prefix: string): string {
  messageSeq += 1;
  return `${prefix}-${Date.now()}-${messageSeq}`;
}

// How many prior turns we ship to the backend for context. Kept short so
// the LLM prompt stays cheap; the backend re-caps this to 8 anyway.
const HISTORY_TURNS = 8;

// SSE endpoint we prefer when the browser supports EventSource. Falls
// back to plain POST when SSE isn't reachable (also nice for debugging).
const STREAM_ENDPOINT = "/v1/chat/stream";

// Best-effort feature detection: EventSource must exist AND the page
// wasn't opened via file:// (which breaks credentials). Server rewrites
// keep /v1 same-origin in dev + prod so we don't need to worry about CORS.
function streamingSupported(): boolean {
  return typeof window !== "undefined" && typeof window.EventSource !== "undefined";
}

function feedbackTargetFromResult(result: ChatResult): Message["feedback"] | undefined {
  if (result?.email_draft) {
    return {
      agent: "EmailAgent",
      field: "email.body",
      value: `${result.email_draft.subject}: ${result.email_draft.body.slice(0, 200)}`,
    };
  }
  if (result?.priority_id) {
    return {
      agent: "PriorityAgent",
      field: "priority.text",
      value: (result.reply || "").slice(0, 200),
    };
  }
  if (result?.reminder_id) {
    return {
      agent: "ReminderAgent",
      field: "reminder.text",
      value: (result.reply || "").slice(0, 200),
    };
  }
  if (result?.person_id) {
    return {
      agent: "PersonEditAgent",
      field: "person.relation",
      value: (result.reply || "").slice(0, 200),
    };
  }
  if (result?.event_id) {
    return {
      agent: "BookAgent",
      field: "booking.title",
      value: (result.reply || "").slice(0, 200),
    };
  }
  return undefined;
}

type ChatResult = {
  reply: string;
  path?: string;
  intent?: string;
  needs_clarification?: boolean;
  clarifying_question?: string | null;
  email_draft?: EmailDraft;
  priority_id?: string;
  reminder_id?: string;
  person_id?: string;
  event_id?: string;
  // Set when the server surfaced remembered priorities in this reply
  // (e.g. booking-conflict confirm bubble). Rendered in teal.
  priority_hits?: string[];
};

function EmailDraftCard({
  draft,
  sent,
  onSent,
}: {
  draft: EmailDraft;
  sent: boolean;
  onSent: () => void;
}) {
  const [subject, setSubject] = useState(draft.subject);
  const [body, setBody] = useState(draft.body);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const locked = sent || busy;

  async function sendDraft() {
    if (sent || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.post(
        "/v1/email/send",
        {
          confirmation_token: draft.confirmation_token,
          to: draft.to,
          subject,
          body,
        },
        { "X-Idempotency-Key": crypto.randomUUID() },
      );
      onSent();
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      // 400 = draft actually expired past its 60-min TTL or token is
      //   unknown (never registered). Only path forward is redraft.
      // 502 = Gmail itself hiccuped; the confirmation token is still
      //   valid server-side (see email.py: token dropped only after
      //   Gmail success). Retry the same Send button click.
      // 409 = same idempotency key submitted twice; the send actually
      //   went through on the first click.
      if (/ApiError 400/.test(detail)) {
        setError("This draft expired. Ask me to write it again.");
      } else if (/ApiError 502/.test(detail)) {
        setError("Gmail didn't accept it just now. Give it another tap.");
      } else if (/ApiError 409/.test(detail)) {
        setError("Already sent - refreshing.");
        onSent();
      } else {
        setError(detail);
      }
    } finally {
      setBusy(false);
    }
  }

  const who = draft.person_name
    ? `${draft.contact_name} · ${draft.person_name}'s ${draft.kind ?? "contact"}`
    : draft.contact_name;

  return (
    <div className={styles.draft} aria-busy={busy}>
      <p className={styles.draftMeta}>
        To {draft.to}
        <span>{who}</span>
      </p>
      <label className={styles.draftField}>
        <span>Subject</span>
        <input
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          disabled={locked}
        />
      </label>
      <label className={styles.draftField}>
        <span>Message</span>
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={7}
          disabled={locked}
        />
      </label>
      <div className={styles.draftStatus} aria-live="polite">
        {busy ? (
          <p className={styles.draftSending}>
            <span className={styles.waitingDots} aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
            Sending to {draft.contact_name}…
          </p>
        ) : null}
        {sent ? (
          <p className={styles.draftSentBanner}>
            Sent to {draft.contact_name} · {draft.to}
          </p>
        ) : null}
        {error ? <p className={styles.draftError}>{error}</p> : null}
      </div>
      {!sent ? (
        <button
          type="button"
          className={busy ? `${styles.draftSend} ${styles.draftSendBusy}` : styles.draftSend}
          onClick={() => void sendDraft()}
          disabled={busy || !subject.trim() || !body.trim()}
        >
          {busy ? "Sending…" : "Send email"}
        </button>
      ) : null}
    </div>
  );
}

/**
 * FeedbackChips — three-button chip row (keep / adjust / not-me) below
 * anything Level generated. Every click POSTs to /v1/feedback which
 * writes a NegativeFeedback row on adjust/not-me; the corresponding
 * agent's next call receives it as few-shot "do not propose this again."
 *
 * We render nothing after a submission, only a small ack line, so the
 * transcript stays readable when the user is scrolling back.
 */
function FeedbackChips({
  target,
  onSubmit,
}: {
  target: NonNullable<Message["feedback"]>;
  onSubmit: (verdict: FeedbackVerdict) => Promise<void>;
}) {
  const submitted = target.verdict;
  const submitting = target.submitting;

  if (submitted) {
    return (
      <p className={styles.feedbackAck}>
        {submitted === "keep"
          ? "Kept — thanks."
          : submitted === "adjust"
            ? "Got it — I’ll adjust next time."
            : "Removed — I won’t propose that again."}
      </p>
    );
  }

  return (
    <div className={styles.feedbackRow} aria-label="Feedback on this reply">
      <span className={styles.feedbackLabel}>How is this?</span>
      {(["keep", "adjust", "not_me"] as FeedbackVerdict[]).map((v) => (
        <button
          key={v}
          type="button"
          className={
            submitting === v
              ? `${styles.feedbackChip} ${styles.feedbackChipActive}`
              : styles.feedbackChip
          }
          disabled={Boolean(submitting)}
          onClick={() => void onSubmit(v)}
        >
          {v === "keep" ? "Keep" : v === "adjust" ? "Adjust" : "Not me"}
        </button>
      ))}
    </div>
  );
}

export default function Chat({
  title = "Ask Level",
  lead,
  placeholder = "Ask about your day, book a time, or draft an email\u2026",
  headerActions,
  onAfterReply,
  onSpeakDay,
  busyHints = DEFAULT_BUSY_HINTS,
}: Props) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [listening, setListening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hintIndex, setHintIndex] = useState(0);
  // Per-request hint override: when the outgoing message looks like
  // an email draft or a booking, we swap the generic "Thinking\u2026"
  // cycle for something specific ("Drafting the email\u2026"). Reset
  // to null at the end of each send so the next message gets the
  // right treatment.
  const [activeHints, setActiveHints] = useState<string[] | null>(null);
  const threadRef = useRef<HTMLDivElement | null>(null);
  const activeEventSource = useRef<EventSource | null>(null);

  const currentHints = activeHints ?? busyHints;

  useEffect(() => {
    if (!busy || currentHints.length === 0) {
      setHintIndex(0);
      return;
    }
    setHintIndex(0);
    const id = window.setInterval(() => {
      setHintIndex((i) => (i + 1) % currentHints.length);
    }, 2200);
    return () => window.clearInterval(id);
  }, [busy, currentHints]);

  useEffect(() => {
    const el = threadRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages, busy]);

  useEffect(() => {
    return () => {
      // Kill any in-flight EventSource on unmount so a cross-page
      // navigation doesn't leak a socket in the browser.
      activeEventSource.current?.close();
      activeEventSource.current = null;
    };
  }, []);

  const submitFeedback = useCallback(
    async (messageId: string, verdict: FeedbackVerdict) => {
      const msg = messages.find((m) => m.id === messageId);
      if (!msg?.feedback) return;
      setMessages((prev) =>
        prev.map((m) =>
          m.id === messageId && m.feedback
            ? { ...m, feedback: { ...m.feedback, submitting: verdict } }
            : m,
        ),
      );
      try {
        await api.post("/v1/feedback", {
          agent: msg.feedback.agent,
          field: msg.feedback.field,
          value: msg.feedback.value,
          verdict,
        });
        setMessages((prev) =>
          prev.map((m) =>
            m.id === messageId && m.feedback
              ? {
                  ...m,
                  feedback: {
                    ...m.feedback,
                    submitting: undefined,
                    verdict,
                  },
                }
              : m,
          ),
        );
      } catch {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === messageId && m.feedback
              ? { ...m, feedback: { ...m.feedback, submitting: undefined } }
              : m,
          ),
        );
      }
    },
    [messages],
  );

  // POST fallback used when SSE isn't supported OR the SSE call fails
  // partway through. Returns the same shape as the streaming path.
  const sendViaPost = useCallback(
    async (text: string, history: Array<{ role: string; text: string }>): Promise<ChatResult> => {
      return await api.post<ChatResult>("/v1/chat", { message: text, history });
    },
    [],
  );

  // Streaming SSE path. Falls back to POST on any error so the user
  // always gets a reply. Progressively updates the last assistant bubble
  // as `delta` events arrive; `done` supplies the final structured
  // payload (email_draft, etc).
  const sendViaSSE = useCallback(
    (
      text: string,
      pendingMessageId: string,
      onFinalize: (result: ChatResult) => void,
      onError: () => void,
    ) => {
      const url = `${STREAM_ENDPOINT}?message=${encodeURIComponent(text)}`;
      const es = new EventSource(url, { withCredentials: true });
      activeEventSource.current = es;
      let final: ChatResult | null = null;

      es.addEventListener("delta", (evt) => {
        try {
          const data = JSON.parse((evt as MessageEvent).data);
          if (typeof data.text === "string") {
            setMessages((prev) =>
              prev.map((m) =>
                m.id === pendingMessageId
                  ? { ...m, text: (m.text ?? "") + data.text, streaming: true }
                  : m,
              ),
            );
          }
        } catch {
          // Malformed frame: ignore; keep listening.
        }
      });

      es.addEventListener("done", (evt) => {
        try {
          final = JSON.parse((evt as MessageEvent).data) as ChatResult;
        } catch {
          final = null;
        }
        es.close();
        activeEventSource.current = null;
        if (final) {
          onFinalize(final);
        } else {
          onError();
        }
      });

      es.onerror = () => {
        es.close();
        activeEventSource.current = null;
        onError();
      };
    },
    [],
  );

  const finalizeMessage = useCallback(
    (pendingId: string, text: string, result: ChatResult) => {
      const feedback = feedbackTargetFromResult(result);
      setMessages((prev) =>
        prev.map((m) =>
          m.id === pendingId
            ? {
                ...m,
                text,
                streaming: false,
                emailDraft: result.email_draft,
                path: result.path,
                intent: result.intent,
                needsClarify: Boolean(result.needs_clarification),
                priorityHits: result.priority_hits?.length ? result.priority_hits : undefined,
                feedback,
              }
            : m,
        ),
      );
    },
    [],
  );

  const send = useCallback(
    async (message: string) => {
      const text = message.trim();
      if (!text || busy) return;
      const userMsg: Message = { id: nextId("u"), role: "user", text };
      const history = messages.slice(-HISTORY_TURNS).map((m) => ({
        role: m.role,
        text: m.text,
      }));
      const pendingId = nextId("a");
      const pendingMsg: Message = {
        id: pendingId,
        role: "assistant",
        text: "",
        streaming: true,
      };
      setMessages((prev) => [...prev, userMsg, pendingMsg]);
      // Pick intent-specific hints for THIS message. Emails go through
      // Gemini Pro with tone + template + Model Armor - a 3-8s ask
      // that felt like silent stalling before. "Drafting the email\u2026"
      // makes the wait feel intentional.
      const specificHints = hintsForMessage(text, busyHints);
      setActiveHints(specificHints === busyHints ? null : specificHints);
      setBusy(true);
      setError(null);
      setDraft("");

      const clearHints = () => setActiveHints(null);

      const removePending = () => {
        setMessages((prev) => prev.filter((m) => m.id !== pendingId && m.id !== userMsg.id));
        setDraft(text);
      };
      const handleFailure = (detail: string) => {
        const friendly = /timeout|aborted|failed to fetch|ApiError 5\d\d/i.test(detail)
          ? "That took longer than expected. Your message is back in the box \u2014 try Send again."
          : detail;
        setError(friendly);
        removePending();
      };

      // Prefer SSE. On any SSE failure, fall back to POST so the user
      // still gets an answer — the pending bubble carries over.
      const finishPost = async () => {
        try {
          const result = await sendViaPost(text, history);
          finalizeMessage(pendingId, result.reply, result);
          onAfterReply?.();
        } catch (err) {
          handleFailure(err instanceof Error ? err.message : String(err));
        } finally {
          setBusy(false);
          clearHints();
        }
      };

      if (streamingSupported()) {
        sendViaSSE(
          text,
          pendingId,
          (result) => {
            finalizeMessage(pendingId, result.reply, result);
            onAfterReply?.();
            setBusy(false);
            clearHints();
          },
          () => {
            void finishPost();
          },
        );
      } else {
        void finishPost();
      }
    },
    [busy, messages, sendViaPost, sendViaSSE, finalizeMessage, onAfterReply, busyHints],
  );

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    void send(draft);
  }

  function toggleMic() {
    if (busy) return;
    if (!voiceSupported.stt) {
      setError("Voice input isn't supported in this browser \u2014 typing works too.");
      return;
    }
    if (listening) {
      setListening(false);
      return;
    }
    setError(null);
    setListening(true);
    listenOnce(
      (text) => {
        setListening(false);
        setDraft((cur) => (cur.trim() ? `${cur.trim()} ${text}` : text));
      },
      () => {
        setListening(false);
        setError("Couldn't hear that. Try again or type instead.");
      },
    );
  }

  async function speakDay() {
    if (onSpeakDay) return onSpeakDay();
    try {
      // Play a Lyria chime (best-effort) BEFORE the TTS starts so the
      // "Hear my day" moment has a warm intro. If Lyria isn't ready
      // (media disabled locally, or first call still generating),
      // we skip silently and go straight to the summary.
      const chime = await api
        .get<{ ready: boolean; audio_url?: string | null }>("/v1/media/chime?mood=calm")
        .catch(() => ({ ready: false as boolean, audio_url: null }));
      if (chime.ready && chime.audio_url) {
        try {
          const audio = new Audio(chime.audio_url);
          audio.volume = 0.6;
          await new Promise<void>((resolve) => {
            audio.onended = () => resolve();
            audio.onerror = () => resolve();
            void audio.play().catch(() => resolve());
            // Absolute ceiling: chime shouldn't hold up TTS more than 4s.
            window.setTimeout(() => resolve(), 4000);
          });
        } catch {
          // Ignore chime failures - never block "Hear my day".
        }
      }
      const { summary } = await api.get<{ summary: string }>("/v1/today/summary");
      speak(summary);
    } catch {
      setError("Couldn't fetch today's summary.");
    }
  }

  const canSubmit = !busy && draft.trim().length > 0;
  const statusLine =
    busy && currentHints.length > 0 ? currentHints[hintIndex % currentHints.length] : null;
  const actions = headerActions ?? (
    <button
      type="button"
      className={styles.headBtn}
      onClick={() => void speakDay()}
      disabled={!voiceSupported.tts}
      aria-label="Hear my day"
      title="Hear my day"
    >
      <SpeakerIcon className={styles.headIcon} />
      Hear my day
    </button>
  );

  // A streaming placeholder that hasn't received tokens yet should
  // render AS the typing bubble (dots + intent hint), not as an
  // empty gray bubble with a caret. That was the "silent empty
  // bubble" pre-fix: we always pushed a pending msg on send, its
  // streaming=true made `anyStreaming` true, and the standalone
  // typing indicator suppressed itself in favor of the empty msg.
  const isEmptyStreaming = (m: Message): boolean =>
    m.role === "assistant" && Boolean(m.streaming) && !m.text.trim();
  const anyStreamingWithText = messages.some(
    (m) => m.streaming && m.text.trim().length > 0,
  );
  const showTrailingTypingIndicator = busy && !messages.some(isEmptyStreaming) && !anyStreamingWithText;

  return (
    <section className={styles.panel} aria-label={title}>
      <div className={styles.head}>
        <h2>{title}</h2>
        {actions}
      </div>
      {lead ? <p className={styles.lead}>{lead}</p> : null}

      <div className={styles.surface}>
        <div
          className={styles.thread}
          ref={threadRef}
          role="log"
          aria-live="polite"
          aria-label="Conversation with Level"
        >
          {messages.length === 0 && !busy ? (
            <p className={styles.emptyHint}>
              Start a chat &mdash; ask about your day, book a time or reminder, or tell me a priority.
            </p>
          ) : null}

          {messages.map((m) => {
            // Empty streaming placeholder: render as typing bubble
            // with the current intent hint. This is the only place
            // that renders while we're still waiting on first tokens
            // - previously it was a blank bubble, which read as a
            // stall.
            if (isEmptyStreaming(m)) {
              const hint = statusLine ?? "Working on it\u2026";
              return (
                <div
                  key={m.id}
                  className={`${styles.bubble} ${styles.bubbleLevel} ${styles.bubbleTyping}`}
                  aria-label={hint.replace(/\u2026$/, "")}
                  aria-live="polite"
                  role="status"
                >
                  <span className={styles.waitingDots} aria-hidden="true">
                    <i />
                    <i />
                    <i />
                  </span>
                  <p className={styles.waitingText}>{hint}</p>
                </div>
              );
            }
            return (
              <div
                key={m.id}
                className={`${styles.bubble} ${
                  m.role === "user" ? styles.bubbleUser : styles.bubbleLevel
                } ${m.emailDraft ? styles.bubbleWide : ""}`}
              >
                <p>
                  {renderReplyWithHighlights(m.text, m.priorityHits)}
                  {m.streaming ? <span className={styles.streamCaret} aria-hidden="true" /> : null}
                </p>
                {m.needsClarify ? (
                  <span className={styles.clarifyChip}>Level is asking</span>
                ) : null}
                {m.emailDraft ? (
                  <EmailDraftCard
                    draft={m.emailDraft}
                    sent={Boolean(m.emailSent)}
                    onSent={() => {
                      const to = m.emailDraft?.contact_name ?? "them";
                      const addr = m.emailDraft?.to;
                      setMessages((prev) => [
                        ...prev.map((row) =>
                          row.id === m.id ? { ...row, emailSent: true } : row,
                        ),
                        {
                          id: nextId("a"),
                          role: "assistant",
                          text: addr ? `Sent to ${to} (${addr}).` : `Sent to ${to}.`,
                        },
                      ]);
                      onAfterReply?.();
                    }}
                  />
                ) : null}
                {m.role === "assistant" && m.feedback && !m.streaming ? (
                  <FeedbackChips
                    target={m.feedback}
                    onSubmit={(verdict) => submitFeedback(m.id, verdict)}
                  />
                ) : null}
              </div>
            );
          })}

          {showTrailingTypingIndicator && statusLine ? (
            <div
              className={`${styles.bubble} ${styles.bubbleLevel} ${styles.bubbleTyping}`}
              aria-label={statusLine.replace(/\u2026$/, "")}
              aria-live="polite"
              role="status"
            >
              <span className={styles.waitingDots} aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
              <p className={styles.waitingText}>{statusLine}</p>
            </div>
          ) : null}
        </div>

        {error ? <p className={styles.error}>{error}</p> : null}

        <form onSubmit={handleSubmit} className={styles.composer}>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={2}
            placeholder={placeholder}
            disabled={busy}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send(draft);
              }
            }}
          />
          <div className={styles.row}>
            <button
              type="button"
              className={styles.voiceBtn}
              onClick={toggleMic}
              disabled={busy || !voiceSupported.stt}
              aria-pressed={listening}
              aria-label={listening ? "Stop voice input" : "Start voice input"}
              title={
                voiceSupported.stt
                  ? listening
                    ? "Stop voice input"
                    : "Start voice input"
                  : "Voice input isn't supported here"
              }
            >
              <MicIcon className={styles.micIcon} />
              {listening ? "Stop" : "Voice"}
            </button>
            <button type="submit" className={styles.submit} disabled={!canSubmit}>
              {busy ? "Sending\u2026" : "Send"}
            </button>
          </div>
        </form>
      </div>
    </section>
  );
}
