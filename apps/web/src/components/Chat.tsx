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
      setError(/ApiError 400/.test(detail) ? "This draft expired. Ask me to write it again." : detail);
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
  const threadRef = useRef<HTMLDivElement | null>(null);
  const activeEventSource = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!busy || busyHints.length === 0) {
      setHintIndex(0);
      return;
    }
    setHintIndex(0);
    const id = window.setInterval(() => {
      setHintIndex((i) => (i + 1) % busyHints.length);
    }, 2200);
    return () => window.clearInterval(id);
  }, [busy, busyHints]);

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
      setBusy(true);
      setError(null);
      setDraft("");

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
          },
          () => {
            void finishPost();
          },
        );
      } else {
        void finishPost();
      }
    },
    [busy, messages, sendViaPost, sendViaSSE, finalizeMessage, onAfterReply],
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
      const { summary } = await api.get<{ summary: string }>("/v1/today/summary");
      speak(summary);
    } catch {
      setError("Couldn't fetch today's summary.");
    }
  }

  const canSubmit = !busy && draft.trim().length > 0;
  const statusLine = busy && busyHints.length > 0 ? busyHints[hintIndex % busyHints.length] : null;
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

  const anyStreaming = messages.some((m) => m.streaming);
  const showTypingIndicator = busy && !anyStreaming;

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

          {messages.map((m) => (
            <div
              key={m.id}
              className={`${styles.bubble} ${
                m.role === "user" ? styles.bubbleUser : styles.bubbleLevel
              } ${m.emailDraft ? styles.bubbleWide : ""}`}
            >
              <p>
                {m.text}
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
          ))}

          {showTypingIndicator && statusLine ? (
            <div
              className={`${styles.bubble} ${styles.bubbleLevel} ${styles.bubbleTyping}`}
              aria-label="Level is thinking"
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
