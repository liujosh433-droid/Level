/**
 * Web Speech API wrapper.
 * Firefox has no SpeechRecognition - `voiceSupported.stt` returns false so
 * the UI can hide the mic button.
 */

type SpeechRecognitionCtor = new () => SpeechRecognition;

interface SpeechRecognition extends EventTarget {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  start: () => void;
  stop: () => void;
  onresult: ((this: SpeechRecognition, ev: SpeechRecognitionEvent) => void) | null;
  onerror: ((this: SpeechRecognition, ev: Event) => void) | null;
  onend: ((this: SpeechRecognition, ev: Event) => void) | null;
}

interface SpeechRecognitionEvent extends Event {
  results: {
    [index: number]: { [index: number]: { transcript: string }; length: number };
    length: number;
  };
}

declare global {
  interface Window {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  }
}

function pickCtor(): SpeechRecognitionCtor | null {
  if (typeof window === "undefined") return null;
  return window.SpeechRecognition ?? window.webkitSpeechRecognition ?? null;
}

export const voiceSupported = {
  get stt(): boolean {
    return pickCtor() !== null;
  },
  get tts(): boolean {
    return typeof window !== "undefined" && "speechSynthesis" in window;
  },
};

export function listenOnce(
  onFinal: (text: string) => void,
  onError?: (msg: string) => void,
): () => void {
  const Ctor = pickCtor();
  if (!Ctor) {
    onError?.("not_supported");
    return () => {};
  }
  const rec = new Ctor();
  rec.lang = "en-US";
  rec.interimResults = false;
  rec.continuous = false;
  rec.onresult = (event) => {
    const first = event.results[0];
    if (first && first[0]) onFinal(first[0].transcript);
  };
  rec.onerror = () => onError?.("recognition_error");
  rec.start();
  return () => rec.stop();
}

export function speak(text: string): void {
  if (typeof window === "undefined" || !("speechSynthesis" in window)) return;
  const utter = new SpeechSynthesisUtterance(text);
  utter.rate = 1.0;
  utter.pitch = 1.0;
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(utter);
}
