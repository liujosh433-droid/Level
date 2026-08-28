"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import styles from "./WeeklyRecap.module.css";

/**
 * 15-second Veo-generated video summarizing this week's caregiving
 * rhythm. Fetches `/v1/media/recap`, which is deterministic +
 * per-ISO-week cached on the backend, so repeat visits are free.
 *
 * Renders one of three states:
 *   - Loading: friendly copy while Veo generates (~30-60s on first hit).
 *   - Ready:   inline <video controls> with an optional Regenerate button.
 *   - Not-ready: static placeholder explaining how to turn it on.
 *
 * A Veo outage or LEVEL_MEDIA_ENABLED=false always resolves to
 * "not-ready" - never to a broken image or a console error - so the
 * page reads cleanly whether or not the feature is enabled in this
 * deployment.
 */

interface RecapResponse {
  ready: boolean;
  reason?: string | null;
  video_url?: string | null;
  poster_url?: string | null;
  week_start?: string | null;
  model?: string | null;
  cached?: boolean;
  generating?: boolean;
  started_at?: string | null;
}

const REASON_COPY: Record<string, string> = {
  media_disabled:
    "Weekly recap video is off in this deployment. Set LEVEL_MEDIA_ENABLED=true and enable Veo 3 in your Vertex project to turn it on.",
  veo_unavailable:
    "Couldn't reach Veo just now. This can happen if the model isn't enabled in your Vertex region or you're out of quota; the next call will retry.",
  veo_no_output:
    "Veo finished but returned no video. Try again in a moment; the model occasionally returns empty on the free-tier preview.",
};

// Poll cadence while generation is in flight. Fast enough that the
// tile flips to a real video within a few seconds of Veo finishing
// (which itself takes 30-60s), slow enough that we don't hammer
// the API for a status that only changes once.
const POLL_INTERVAL_MS = 4000;
// Overall polling ceiling. Veo previews rarely exceed 90s; anything
// longer we surface as a soft-fail so the user can retry rather than
// stare at a spinner forever.
const POLL_TIMEOUT_MS = 120_000;

function reasonCopy(reason: string | null | undefined): string {
  if (!reason) return REASON_COPY.veo_unavailable;
  return REASON_COPY[reason] ?? REASON_COPY.veo_unavailable;
}

function isTerminalReason(reason: string | null | undefined): boolean {
  if (!reason) return true;
  return reason !== "generating";
}

function weekLabel(iso: string | null | undefined): string {
  if (!iso) return "";
  const start = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(start.getTime())) return "";
  const end = new Date(start);
  end.setDate(end.getDate() + 6);
  const fmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
  return `Week of ${fmt.format(start)} - ${fmt.format(end)}`;
}

export default function WeeklyRecap() {
  const [state, setState] = useState<
    | { kind: "idle" }
    | { kind: "loading"; force: boolean }
    | { kind: "generating"; startedAt: string | null }
    | { kind: "ready"; data: RecapResponse }
    | { kind: "not_ready"; data: RecapResponse }
    | { kind: "error"; message: string }
  >({ kind: "idle" });

  const abortRef = useRef<AbortController | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollDeadlineRef = useRef<number | null>(null);

  const clearPoll = useCallback(() => {
    if (pollTimerRef.current !== null) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    pollDeadlineRef.current = null;
  }, []);

  const fetchRecap = useCallback(
    async (mode: "initial" | "force" | "poll") => {
      // Force + initial fetches replace any in-flight request; poll
      // fetches piggy-back on the same slot so a reload during a
      // poll cycle doesn't double up.
      if (mode !== "poll") {
        abortRef.current?.abort();
        clearPoll();
      }
      const ctl = new AbortController();
      abortRef.current = ctl;

      if (mode === "force") {
        setState({ kind: "loading", force: true });
      } else if (mode === "initial") {
        setState({ kind: "loading", force: false });
      }
      // For polls we intentionally leave the previous "generating"
      // state in place so the UI doesn't flicker back to a spinner.

      try {
        const path = mode === "force" ? "/v1/media/recap?force=true" : "/v1/media/recap";
        const data = await api.get<RecapResponse>(path);
        if (ctl.signal.aborted) return;

        if (data.ready && data.video_url) {
          clearPoll();
          setState({ kind: "ready", data });
          return;
        }

        // Server told us a background task is running. Show the
        // "generating" state and start (or continue) polling. Force
        // path never returns generating - it blocks synchronously
        // on the SDK - so we don't need to schedule a poll there.
        if (data.generating || data.reason === "generating") {
          if (mode !== "poll") {
            pollDeadlineRef.current = Date.now() + POLL_TIMEOUT_MS;
          }
          setState({ kind: "generating", startedAt: data.started_at ?? null });
          const deadline = pollDeadlineRef.current;
          if (deadline !== null && Date.now() >= deadline) {
            clearPoll();
            setState({
              kind: "not_ready",
              data: { ...data, reason: "veo_unavailable" },
            });
            return;
          }
          pollTimerRef.current = setTimeout(() => {
            void fetchRecap("poll");
          }, POLL_INTERVAL_MS);
          return;
        }

        // Terminal not-ready reason (media_disabled, veo_no_output,
        // veo_unavailable). Stop polling and render the placeholder.
        clearPoll();
        if (isTerminalReason(data.reason)) {
          setState({ kind: "not_ready", data });
        }
      } catch (err) {
        if (ctl.signal.aborted) return;
        clearPoll();
        setState({
          kind: "error",
          message: err instanceof Error ? err.message : "Something went wrong",
        });
      }
    },
    [clearPoll],
  );

  useEffect(() => {
    void fetchRecap("initial");
    return () => {
      abortRef.current?.abort();
      clearPoll();
    };
  }, [fetchRecap, clearPoll]);

  return (
    <section className={styles.wrap} aria-label="Weekly recap video">
      <header className={styles.header}>
        <div>
          <h2 className={styles.title}>This week&apos;s recap</h2>
          <p className={styles.subtitle}>
            A 15-second cinematic loop of your week - generated once, cached per ISO week.
            Category labels only; no names or event bodies leave your account for the video prompt.
          </p>
        </div>
        {state.kind === "ready" && (
          <button
            type="button"
            className={styles.regenerate}
            onClick={() => void fetchRecap("force")}
          >
            Regenerate
          </button>
        )}
      </header>

      {state.kind === "loading" && (
        <div className={styles.placeholder} role="status" aria-live="polite">
          <div className={styles.spinner} aria-hidden />
          <p>
            {state.force ? "Regenerating" : "Loading"} this week&apos;s recap...
          </p>
        </div>
      )}

      {state.kind === "generating" && (
        <div className={styles.placeholder} role="status" aria-live="polite">
          <div className={styles.spinner} aria-hidden />
          <p>
            Cooking this week&apos;s recap in the background. Veo 3 usually takes 30-60 seconds -
            feel free to keep exploring; the tile will update on its own when it&apos;s ready.
          </p>
        </div>
      )}

      {state.kind === "ready" && (
        <figure className={styles.videoFrame}>
          <video
            className={styles.video}
            src={state.data.video_url ?? undefined}
            poster={state.data.poster_url ?? undefined}
            controls
            playsInline
            preload="metadata"
          />
          <figcaption className={styles.caption}>
            {weekLabel(state.data.week_start)}
            {state.data.model ? ` \u00b7 ${state.data.model}` : ""}
            {state.data.cached ? " \u00b7 cached" : ""}
          </figcaption>
        </figure>
      )}

      {state.kind === "not_ready" && (
        <div className={styles.placeholder}>
          <span className={styles.placeholderBadge}>Off</span>
          <p>{reasonCopy(state.data.reason)}</p>
        </div>
      )}

      {state.kind === "error" && (
        <div className={styles.placeholder}>
          <span className={styles.placeholderBadge}>Error</span>
          <p>{state.message}</p>
          <button
            type="button"
            className={styles.regenerate}
            onClick={() => void fetchRecap("initial")}
          >
            Try again
          </button>
        </div>
      )}
    </section>
  );
}
