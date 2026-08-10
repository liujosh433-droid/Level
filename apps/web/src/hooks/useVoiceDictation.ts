"use client";

import { useCallback, useRef, useState } from "react";

/** Browser speech-to-text for Ask / Tell Level drafts. */
export function useVoiceDictation(
  onTranscript: (text: string) => void,
  onUnsupported?: (message: string) => void,
) {
  const [listening, setListening] = useState(false);
  const recognitionRef = useRef<SpeechRecognition | null>(null);

  const toggle = useCallback(() => {
    const SR =
      typeof window !== "undefined"
        ? window.SpeechRecognition || window.webkitSpeechRecognition
        : undefined;
    if (!SR) {
      onUnsupported?.(
        "Voice isn’t supported in this browser — try Chrome, or type instead.",
      );
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
      if (text) onTranscript(text);
    };
    rec.onerror = () => setListening(false);
    rec.onend = () => setListening(false);
    setListening(true);
    rec.start();
  }, [listening, onTranscript, onUnsupported]);

  return { listening, toggleVoice: toggle };
}
