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

type Message = {
  id: string;
  role: "user" | "assistant";
  text: string;
  emailDraft?: EmailDraft;
  emailSent?: boolean;
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

function nextId(prefix: string): string {
  messageSeq += 1;
  return `${prefix}-${Date.now()}-${messageSeq}`;
}

// How many prior turns we ship to the backend for context. Kept short so
// the LLM prompt stays cheap; the backend re-caps this to 8 anyway.
const HISTORY_TURNS = 8;

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
  }, [messages.length, busy]);

  const send = useCallback(
    async (message: string) => {
      const text = message.trim();
      if (!text || busy) return;
      const userMsg: Message = { id: nextId("u"), role: "user", text };
      // Snapshot of history at send-time, BEFORE we append the new user
      // message, so the backend sees "prior_turns" that lead up to `text`.
      const history = messages.slice(-HISTORY_TURNS).map((m) => ({
        role: m.role,
        text: m.text,
      }));
      setMessages((prev) => [...prev, userMsg]);
      setBusy(true);
      setError(null);
      setDraft("");
      try {
        const res = await api.post<{ reply: string; email_draft?: EmailDraft }>(
          "/v1/chat",
          {
            message: text,
            history,
          },
        );
        setMessages((prev) => [
          ...prev,
          {
            id: nextId("a"),
            role: "assistant",
            text: res.reply,
            emailDraft: res.email_draft,
          },
        ]);
        onAfterReply?.();
      } catch (err) {
        const detail = err instanceof Error ? err.message : String(err);
        const friendly = /timeout|aborted|failed to fetch|ApiError 5\d\d/i.test(detail)
          ? "That took longer than expected. Your message is back in the box \u2014 try Send again."
          : detail;
        setError(friendly);
        setDraft(text);
        setMessages((prev) => prev.filter((m) => m.id !== userMsg.id));
      } finally {
        setBusy(false);
      }
    },
    [busy, messages, onAfterReply],
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
              Start a chat &mdash; ask about your day, book a time, or tell me a priority.
            </p>
          ) : null}

          {messages.map((m) => (
            <div
              key={m.id}
              className={`${styles.bubble} ${
                m.role === "user" ? styles.bubbleUser : styles.bubbleLevel
              } ${m.emailDraft ? styles.bubbleWide : ""}`}
            >
              <p>{m.text}</p>
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
            </div>
          ))}

          {busy && statusLine ? (
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
