"use client";

import { FormEvent, ReactNode, useCallback, useEffect, useRef } from "react";
import { useVoiceDictation } from "@/hooks/useVoiceDictation";
import styles from "./TellLevelPanel.module.css";

export function TellLevelPanel({
  title = "Tell Level more",
  lead,
  placeholder,
  value,
  onChange,
  onSubmit,
  busy = false,
  disabled = false,
  submitLabel = "Send",
  busyLabel = "Listening…",
  minLength = 1,
  error,
  headerActions,
  voiceEnabled = false,
  onVoiceError,
  children,
}: {
  title?: string;
  lead?: string;
  placeholder: string;
  value: string;
  onChange: (next: string) => void;
  onSubmit: (text: string) => void | Promise<void>;
  busy?: boolean;
  disabled?: boolean;
  submitLabel?: string;
  busyLabel?: string;
  minLength?: number;
  error?: string | null;
  headerActions?: ReactNode;
  voiceEnabled?: boolean;
  onVoiceError?: (message: string) => void;
  children?: ReactNode;
}) {
  const valueRef = useRef(value);
  useEffect(() => {
    valueRef.current = value;
  }, [value]);

  const appendTranscript = useCallback(
    (text: string) => {
      const cur = valueRef.current.trim();
      onChange(cur ? `${cur} ${text}` : text);
    },
    [onChange],
  );

  const { listening, toggleVoice } = useVoiceDictation(
    appendTranscript,
    onVoiceError,
  );

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const text = value.trim();
    if (busy || disabled || text.length < minLength) return;
    await onSubmit(text);
  }

  const canSubmit = !busy && !disabled && value.trim().length >= minLength;

  return (
    <section className={styles.panel}>
      <div className={styles.head}>
        <h2>{title}</h2>
        {headerActions}
      </div>
      {lead ? <p className={styles.lead}>{lead}</p> : null}
      <div className={styles.dock}>
        <form onSubmit={(e) => void handleSubmit(e)} className={styles.form}>
          {error ? <p className={styles.error}>{error}</p> : null}
          <textarea
            value={value}
            onChange={(e) => onChange(e.target.value)}
            rows={3}
            placeholder={placeholder}
            disabled={busy || disabled}
          />
          <div className={styles.row}>
            {voiceEnabled ? (
              <button
                type="button"
                className={styles.voiceBtn}
                onClick={toggleVoice}
                disabled={busy || disabled}
                aria-pressed={listening}
                aria-label={listening ? "Stop voice input" : "Start voice input"}
              >
                <svg
                  className={styles.micIcon}
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
                {listening ? "Stop" : "Voice"}
              </button>
            ) : null}
            <button type="submit" disabled={!canSubmit}>
              {busy ? busyLabel : submitLabel}
            </button>
          </div>
        </form>
      </div>
      {children != null && children !== false ? (
        <div className={styles.thread}>{children}</div>
      ) : null}
    </section>
  );
}

export function TellLevelYou({ children }: { children: ReactNode }) {
  return <p className={styles.you}>{children}</p>;
}

export function TellLevelReply({ children }: { children: ReactNode }) {
  return (
    <div className={styles.level}>
      {typeof children === "string" ? <p>{children}</p> : children}
    </div>
  );
}
