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

type ChatItem = {
  id: string;
  you: string;
  reply: string;
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
  "Looking at your calendar\u2026",
  "Weighing what this would crowd out\u2026",
  "Checking your care load\u2026",
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

export default function Chat({
  title = "Ask Level",
  lead,
  placeholder = "Ask about your day, book a time, or draft an email\u2026",
  headerActions,
  onAfterReply,
  onSpeakDay,
  busyHints = DEFAULT_BUSY_HINTS,
}: Props) {
  const [items, setItems] = useState<ChatItem[]>([]);
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
    threadRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [items.length, busy]);

  const send = useCallback(
    async (message: string) => {
      const text = message.trim();
      if (!text || busy) return;
      const id = `${Date.now()}`;
      setBusy(true);
      setError(null);
      setDraft("");
      try {
        const res = await api.post<{ reply: string }>("/v1/chat", { message: text });
        setItems((prev) => [...prev, { id, you: text, reply: res.reply }]);
        onAfterReply?.();
      } catch (err) {
        const detail = err instanceof Error ? err.message : String(err);
        setError(detail);
        setItems((prev) => [
          ...prev,
          {
            id,
            you: text,
            reply: "Sorry \u2014 something interrupted me. Try again in a moment?",
          },
        ]);
      } finally {
        setBusy(false);
      }
    },
    [busy, onAfterReply],
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

      <div className={styles.composer}>
        <form onSubmit={handleSubmit} className={styles.form}>
          {error ? <p className={styles.error}>{error}</p> : null}
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={3}
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
              {busy ? "Thinking\u2026" : "Send"}
            </button>
          </div>
        </form>
      </div>

      {items.length > 0 ? (
        <div className={styles.thread} ref={threadRef}>
          {items.map((it) => (
            <article key={it.id} className={styles.turn}>
              <p className={styles.you}>{it.you}</p>
              <div className={styles.level}>
                <p>{it.reply}</p>
              </div>
            </article>
          ))}
        </div>
      ) : null}

      {statusLine ? (
        <div className={styles.waiting} role="status" aria-live="polite">
          <span className={styles.waitingDots} aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <p className={styles.waitingText}>{statusLine}</p>
        </div>
      ) : null}
    </section>
  );
}
