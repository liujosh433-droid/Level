"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import styles from "./WeeklyRecap.module.css";

/**
 * 8-second Veo-generated video summarizing this week's caregiving
 * rhythm. Fetches `/v1/media/recap`, which is deterministic +
 * per-ISO-week cached on the backend, so repeat visits are free.
 *
 * Renders one of four states:
 *   - Loading: instant transient state before the first fetch resolves.
 *   - Generating: backend has a background Veo task in flight; frontend
 *     polls every POLL_INTERVAL_MS and displays elapsed time. After
 *     SLOW_HINT_ELAPSED_MS the copy softens to "taking longer than
 *     usual" so a slow Veo run doesn't feel broken.
 *   - Ready: inline <video controls> with an optional Regenerate button.
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
  regenerations_used?: number | null;
  regenerations_max?: number | null;
}

const REASON_COPY: Record<string, string> = {
  media_disabled:
    "Weekly recap video is off in this deployment. Set LEVEL_MEDIA_ENABLED=true and enable Veo 3.1 in your Vertex project to turn it on.",
  veo_unavailable:
    "Couldn't reach Veo just now. This can happen if the model isn't enabled in your Vertex region or you're out of quota; the next call will retry.",
  veo_no_output:
    "Veo finished but returned no video. Try again in a moment; the model occasionally returns empty on the free-tier preview.",
  regeneration_limit_reached:
    "You've used this week's regeneration budget. This demo runs on a bounded Veo credit pool (~$1.20 per generation on Veo 3.1 Fast), so we cap regenerations per user per week. The recap will refresh automatically on Monday.",
};

// Poll cadence while generation is in flight. 6s is fast enough
// that the tile flips within a few seconds of Veo finishing, slow
// enough to keep the request rate low so a user leaving /week
// open doesn't churn Cloud Run request slots.
const POLL_INTERVAL_MS = 6000;
// Overall polling ceiling. Must match the backend's Veo polling
// ceiling (VEO_POLL_CEILING_SECONDS = 600s) so the frontend gives
// up at roughly the same moment the background task does - not
// sooner (leaves the user staring at a "failed" tile while the
// backend is still working) and not later (leaves the tile
// pretending to generate after the task has already given up).
const POLL_TIMEOUT_MS = 600_000;
// Elapsed threshold past which we swap the "usually 1-3 min"
// copy for a "taking longer than usual" hint. Fired at the P50
// upper bound so the copy stays accurate through the typical
// generation window and only softens when we're genuinely slow.
const SLOW_HINT_ELAPSED_MS = 180_000;

function elapsedSeconds(startedAt: string | null): number | null {
  if (!startedAt) return null;
  const started = Date.parse(startedAt);
  if (Number.isNaN(started)) return null;
  return Math.max(0, Math.round((Date.now() - started) / 1000));
}

function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const min = Math.floor(seconds / 60);
  const rem = seconds % 60;
  return rem === 0 ? `${min}m` : `${min}m ${rem}s`;
}

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
      // Poll cycles first check the deadline BEFORE issuing the
      // HTTP request. If we're past the ceiling, don't fire another
      // poll - a poll to a backend whose in-flight flag has just
      // aged out will spawn a fresh Veo task (wasted spend) right
      // as the tile is about to flip to "unavailable" anyway.
      if (mode === "poll") {
        const deadline = pollDeadlineRef.current;
        if (deadline !== null && Date.now() >= deadline) {
          clearPoll();
          setState({
            kind: "not_ready",
            data: { ready: false, reason: "veo_unavailable" } as RecapResponse,
          });
          return;
        }
      }

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
            // Anchor the poll deadline to the SERVER'S reported
            // started_at when we have it, so a reload mid-generation
            // doesn't reset the client-side clock and let the user
            // wait 2x the intended ceiling. Falls back to Date.now()
            // when started_at is missing or malformed.
            const serverStart = data.started_at ? Date.parse(data.started_at) : NaN;
            const base = Number.isFinite(serverStart) ? serverStart : Date.now();
            pollDeadlineRef.current = base + POLL_TIMEOUT_MS;
          }
          setState({ kind: "generating", startedAt: data.started_at ?? null });
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
            An 8-second cinematic loop of your week - generated once, cached per ISO week.
            Category labels only; no names or event bodies leave your account for the video prompt.
          </p>
        </div>
        {state.kind === "ready" && (() => {
          const used = state.data.regenerations_used ?? 0;
          const max = state.data.regenerations_max ?? 0;
          const remaining = Math.max(0, max - used);
          const canRegen = max === 0 || remaining > 0;
          return (
            <div className={styles.regenerateBox}>
              <button
                type="button"
                className={styles.regenerate}
                onClick={() => void fetchRecap("force")}
                disabled={!canRegen}
                title={
                  canRegen
                    ? `${remaining} of ${max} regenerations left this week`
                    : `Regeneration budget for this week is used up`
                }
              >
                Regenerate
              </button>
              {max > 0 ? (
                <span className={styles.quotaNote}>
                  {canRegen
                    ? `${remaining}/${max} left this week`
                    : `0/${max} left - resets Monday`}
                </span>
              ) : null}
            </div>
          );
        })()}
      </header>

      {state.kind === "loading" && (
        <div className={styles.placeholder} role="status" aria-live="polite">
          <div className={styles.spinner} aria-hidden />
          <p>
            {state.force ? "Regenerating" : "Loading"} this week&apos;s recap...
          </p>
        </div>
      )}

      {state.kind === "generating" && (() => {
        const elapsed = elapsedSeconds(state.startedAt);
        const slow = elapsed !== null && elapsed * 1000 >= SLOW_HINT_ELAPSED_MS;
        return (
          <div className={styles.placeholder} role="status" aria-live="polite">
            <div className={styles.spinner} aria-hidden />
            <p>
              {slow ? (
                <>
                  Still cooking this week&apos;s recap - Veo is running slower than usual today.
                  It&apos;ll finish on its own within a few more minutes; feel free to close this
                  tab and come back to <em>/week</em> later - the video will be waiting.
                </>
              ) : (
                <>
                  Cooking this week&apos;s recap in the background. Veo usually takes 1-3 minutes,
                  occasionally longer under peak load - the tile will update on its own when
                  it&apos;s ready. Feel free to keep exploring or close this tab; the recap will be
                  cached and instant on your next visit.
                </>
              )}
              {elapsed !== null ? (
                <>
                  {" "}
                  <span className={styles.elapsedNote}>Elapsed: {formatElapsed(elapsed)}.</span>
                </>
              ) : null}
            </p>
          </div>
        );
      })()}

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

      {state.kind === "not_ready" && (() => {
        // Recoverable Veo failures (transient outage, empty output)
        // get a "Try again" button that fires force=true. The
        // backend cooldown gates the automatic poll cycle but
        // an explicit user action bypasses it - that's the whole
        // point of exposing the button. Suppressed for
        // media_disabled (server config, retry can't help) and
        // regeneration_limit_reached (waiting is the answer).
        const retryable =
          state.data.reason === "veo_unavailable" ||
          state.data.reason === "veo_no_output";
        const used = state.data.regenerations_used ?? 0;
        const max = state.data.regenerations_max ?? 0;
        const hasBudget = max === 0 || used < max;
        return (
          <div className={styles.placeholder}>
            <span className={styles.placeholderBadge}>
              {state.data.reason === "regeneration_limit_reached"
                ? "Budget reached"
                : retryable
                  ? "Unavailable"
                  : "Off"}
            </span>
            <p>{reasonCopy(state.data.reason)}</p>
            {retryable && hasBudget ? (
              <button
                type="button"
                className={styles.regenerate}
                onClick={() => void fetchRecap("force")}
              >
                Try again
              </button>
            ) : null}
          </div>
        );
      })()}

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
