"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Chat from "@/components/Chat";
import EventCard from "@/components/EventCard";
import RemindersPanel from "@/components/RemindersPanel";
import RoleLoadBar from "@/components/RoleLoadBar";
import { api, ApiError } from "@/lib/api";
import { browserTimeZone, formatDateOnly } from "@/lib/dates";
import { buildPersonColorMap } from "@/lib/personColor";
import type {
  CalendarSyncInfo,
  DemoScenario,
  FeaturesResponse,
  MissingUsualWeek,
  ProactiveCard,
  TodayResponse,
  WhoAmI,
} from "@/lib/types";
import styles from "./today.module.css";

const WEEKDAY_LABEL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

const RELATION_LABEL: Record<string, string> = {
  self: "You",
  child: "Kid",
  elder: "Elder",
  coparent: "Co-parent",
  partner: "Partner",
  friend: "Friend",
  other: "Other",
};

function groupMissingByWeekday(items: MissingUsualWeek[]): { weekday: number; items: MissingUsualWeek[] }[] {
  const buckets = new Map<number, MissingUsualWeek[]>();
  for (const m of items) {
    const arr = buckets.get(m.weekday) ?? [];
    arr.push(m);
    buckets.set(m.weekday, arr);
  }
  return Array.from(buckets.entries())
    .sort(([a], [b]) => a - b)
    .map(([weekday, arr]) => ({
      weekday,
      items: arr.sort((a, b) => (a.typical_start ?? "").localeCompare(b.typical_start ?? "")),
    }));
}

function EmptyCalendarHint({
  when,
  sync,
}: {
  when: "today" | "tomorrow";
  sync?: CalendarSyncInfo;
}) {
  if (sync?.pulling && !sync.total_cached) {
    return <p className={styles.meta}>Pulling your calendar&hellip;</p>;
  }
  if (sync?.last_error) {
    return (
      <p className={styles.meta}>
        Couldn&apos;t read Google Calendar: {sync.last_error}. Try{" "}
        <a href="/sources">Sources → Sync now</a>.
      </p>
    );
  }
  if (!sync?.total_cached) {
    const names = (sync?.calendars ?? [])
      .map((c) => c.summary)
      .filter((name): name is string => Boolean(name));
    return (
      <p className={styles.meta}>
        Nothing in the cache yet
        {names.length ? ` (checked ${names.join(", ")})` : ""}. Reload this page, or tap{" "}
        <a href="/sources">Sync now</a>.
      </p>
    );
  }
  return (
    <p className={styles.meta}>
      {when === "today" ? "Nothing on the calendar." : "Nothing on the calendar yet."}
    </p>
  );
}

/**
 * Onboarding progress card. Renders on first-connect while the OAuth
 * callback's sync refresh + background enrich are still in flight.
 * Communicates progress instead of a generic "Loading..." spinner - this
 * is where new users spend their first few seconds with Level.
 */
function OnboardingProgress({ step }: { step: "connecting" | "pulling" }) {
  const pullingActive = step === "pulling";
  return (
    <section className={styles.onboardingCard}>
      <h1>Welcome to Level</h1>
      <p>Level is getting set up. This usually takes about 5 seconds.</p>
      <ul className={styles.onboardingSteps}>
        <li className={styles.done}>
          <span className={styles.stepDot} />
          Connected to Google
        </li>
        <li className={pullingActive ? styles.done : ""}>
          <span
            className={`${styles.stepDot} ${!pullingActive ? styles.active : ""}`}
          />
          Pulling your calendar
        </li>
        <li>
          <span className={`${styles.stepDot} ${pullingActive ? styles.active : ""}`} />
          Recognizing your usual rhythm
        </li>
      </ul>
    </section>
  );
}

export default function TodayPage() {
  const [who, setWho] = useState<WhoAmI | null>(null);
  const [data, setData] = useState<TodayResponse | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [needsConnect, setNeedsConnect] = useState(false);
  const [remindersTick, setRemindersTick] = useState(0);
  const [dismissMissing, setDismissMissing] = useState(false);
  const [resolvedIds, setResolvedIds] = useState<Set<string>>(() => new Set());
  const [resolvingId, setResolvingId] = useState<string | null>(null);

  const [loadError, setLoadError] = useState<string | null>(null);
  const [dismissedCardIds, setDismissedCardIds] = useState<Set<string>>(() => new Set());
  const [features, setFeatures] = useState<FeaturesResponse | null>(null);
  const [demoLoadingScenario, setDemoLoadingScenario] = useState<
    "family" | "solo" | null
  >(null);
  const [demoError, setDemoError] = useState<string | null>(null);
  const emptyPolls = useRef(0);

  const load = useCallback(async () => {
    try {
      const me = await api.get<WhoAmI>("/v1/me");
      setWho(me);
      if (!me.google_connected) {
        setNeedsConnect(true);
        setData(null);
        return;
      }
      const tz = browserTimeZone();
      if (me.tz !== tz) {
        void api.patch<WhoAmI>("/v1/me", { tz }).then((next) => setWho(next)).catch(() => undefined);
      }
      const today = await api.get<TodayResponse>(`/v1/today?tz=${encodeURIComponent(tz)}`);
      setData(today);
      setNeedsConnect(false);
      setLoadError(null);
      setDismissMissing(Boolean(today.missing_usuals_week_dismissed));
      // Note: "What Level learned" used to render here as a strip.
      // Removed - the same signal (people + usuals + corrections) is
      // already visible in the /profile sidebar, and stacking it on
      // /today made the home page feel crowded.
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setNeedsConnect(true);
        return;
      }
      setLoadError(err instanceof ApiError ? err.detail : "Couldn’t load today.");
    }
  }, []);

  useEffect(() => {
    void load().finally(() => setInitialLoading(false));
  }, [load]);

  useEffect(() => {
    void api
      .get<FeaturesResponse>("/v1/config/features")
      .then(setFeatures)
      .catch(() => setFeatures(null));
  }, []);

  const startDemo = useCallback(
    async (scenarioId: "family" | "solo") => {
      setDemoError(null);
      setDemoLoadingScenario(scenarioId);
      try {
        await api.post("/v1/auth/demo", { scenario: scenarioId });
        // Full reload so every page state (chat, sidebar, whoami event
        // listeners) restarts against the seeded demo user.
        window.location.reload();
      } catch (err) {
        setDemoLoadingScenario(null);
        setDemoError(
          err instanceof ApiError
            ? err.detail
            : "Couldn't start demo mode. Try again?",
        );
      }
    },
    [],
  );

  useEffect(() => {
    function onWho(event: Event) {
      const me = (event as CustomEvent<WhoAmI>).detail;
      if (me) setWho(me);
    }
    window.addEventListener("level:whoami", onWho);
    return () => window.removeEventListener("level:whoami", onWho);
  }, []);

  useEffect(() => {
    const empty = Boolean(data) && (data?.sync?.total_cached ?? 0) === 0 && !data?.sync?.last_error;
    if (!empty) {
      emptyPolls.current = 0;
      return;
    }
    if (emptyPolls.current >= 12) return;
    emptyPolls.current += 1;
    const t = window.setTimeout(() => void load(), 1200);
    return () => window.clearTimeout(t);
  }, [data, load]);

  const missingByDay = useMemo(
    () =>
      groupMissingByWeekday(
        (data?.missing_usuals_week ?? []).filter((m) => !resolvedIds.has(m.group_id)),
      ),
    [data?.missing_usuals_week, resolvedIds],
  );

  const colorMap = useMemo(() => {
    const ids = (data?.missing_usuals_week ?? []).flatMap((m) =>
      m.people?.length ? m.people.map((p) => p.person_id) : [m.person_id],
    );
    return buildPersonColorMap(ids);
  }, [data?.missing_usuals_week]);

  if (initialLoading && !data) {
    return <OnboardingProgress step="connecting" />;
  }
  // Post-connect, pre-first-pull: agenda cache is empty but we know a
  // background pull is in flight. Show the same rich progress card
  // instead of the generic "Loading today..." fallback.
  if (
    data &&
    (data.sync?.total_cached ?? 0) === 0 &&
    !data.sync?.last_error &&
    !needsConnect
  ) {
    return <OnboardingProgress step="pulling" />;
  }
  if (loadError && !data) {
    return (
      <section className={styles.empty}>
        <h1>Couldn&apos;t load today</h1>
        <p className={styles.meta}>{loadError}</p>
      </section>
    );
  }
  if (needsConnect) {
    const demoAvailable = features?.demo?.available === true;
    const scenarios: DemoScenario[] = features?.demo?.scenarios ?? [];
    const primary = scenarios.find((s) => s.id === "family");
    const secondary = scenarios.find((s) => s.id === "solo");
    return (
      <section className={styles.empty}>
        <h1>Connect Google to get started</h1>
        <p className={styles.meta}>
          Level reads your calendar so it can spot what&apos;s usual and what&apos;s missing.
        </p>
        <a className="button-primary" href="/v1/auth/google/start">
          Connect Google
        </a>
        {demoAvailable ? (
          <>
            <p className={styles.demoDivider}>
              <span>or skip OAuth &mdash; try a demo</span>
            </p>
            <div className={styles.demoRow}>
              {primary ? (
                <button
                  type="button"
                  className={styles.demoButton}
                  onClick={() => void startDemo("family")}
                  disabled={demoLoadingScenario !== null}
                  aria-busy={demoLoadingScenario === "family"}
                >
                  {demoLoadingScenario === "family"
                    ? "Loading demo\u2026"
                    : `Try demo: ${primary.label}`}
                </button>
              ) : null}
              {secondary ? (
                <button
                  type="button"
                  className={styles.demoLink}
                  onClick={() => void startDemo("solo")}
                  disabled={demoLoadingScenario !== null}
                  aria-busy={demoLoadingScenario === "solo"}
                >
                  {demoLoadingScenario === "solo"
                    ? "Loading\u2026"
                    : `Or: ${secondary.label}`}
                </button>
              ) : null}
            </div>
            {primary ? (
              <p className={styles.demoTagline}>{primary.tagline}</p>
            ) : null}
            {demoError ? (
              <p className={styles.demoError} role="alert">
                {demoError}
              </p>
            ) : null}
          </>
        ) : null}
      </section>
    );
  }

  const dateLabel = data
    ? formatDateOnly(data.date, {
        weekday: "long",
        month: "short",
        day: "numeric",
      })
    : null;
  const savedName = who?.display_name?.trim() ?? "";
  const greetingName =
    savedName && !/^(you|me|self|myself|a parent)$/i.test(savedName)
      ? savedName
      : (who?.email?.split("@")[0] ?? "there");
  const weekday = data ? formatDateOnly(data.date, { weekday: "long" }) : null;

  const hasMissing = !dismissMissing && missingByDay.length > 0;
  const activeCards = (data?.proactive_cards ?? []).filter(
    (c) => !dismissedCardIds.has(c.card_id),
  );

  async function dismissMissingWeek() {
    setDismissMissing(true);
    try {
      await api.post("/v1/today/missing-week/dismiss", {});
    } catch {
      setDismissMissing(false);
    }
  }

  async function dismissProactiveCard(cardId: string) {
    setDismissedCardIds((prev) => new Set(prev).add(cardId));
    try {
      await api.post("/v1/today/proactive-cards/dismiss", { card_id: cardId });
    } catch {
      setDismissedCardIds((prev) => {
        const next = new Set(prev);
        next.delete(cardId);
        return next;
      });
    }
  }

  async function resolveMissingGroup(groupId: string) {
    setResolvingId(groupId);
    setResolvedIds((prev) => new Set(prev).add(groupId));
    try {
      await api.post("/v1/today/missing-week/resolve", { group_id: groupId });
    } catch {
      setResolvedIds((prev) => {
        const next = new Set(prev);
        next.delete(groupId);
        return next;
      });
    } finally {
      setResolvingId(null);
    }
  }

  return (
    <div className={styles.wrap}>
      <div className={styles.workspace}>
        <div className={styles.mainCol}>
          <header className={styles.hero}>
            {dateLabel && <p className={styles.dateLabel}>{dateLabel}</p>}
            <h1>
              Hi {greetingName}
              {weekday ? `, Happy ${weekday}` : ""}!
            </h1>
            <p className={styles.sub}>
              {data?.today.length
                ? `${data.today.length} on your calendar today.`
                : data?.sync?.total_cached
                  ? "Your calendar is clear today."
                  : "Still looking for events on your calendars."}
            </p>
            <RoleLoadBar load={data?.week_load} />
          </header>

          {activeCards.length > 0 && (
            <section
              className={`${styles.block} ${styles.proactive}`}
              aria-label="Level noticed"
            >
              <div className={styles.proactiveHead}>
                <h2>Level noticed while you slept</h2>
                <p className={styles.missingHint}>
                  Autonomous nudges from the nightly job. Tap a card to act, or dismiss to hide until next week.
                </p>
              </div>
              <ul className={styles.proactiveList}>
                {activeCards.map((card: ProactiveCard) => (
                  <li key={card.card_id} className={styles.proactiveCard}>
                    <div>
                      <p className={styles.proactiveText}>{card.text}</p>
                      <p className={styles.meta}>{card.category_label} &middot; {card.day}</p>
                    </div>
                    <div className={styles.proactiveActions}>
                      <button
                        type="button"
                        className="button-primary"
                        onClick={() => {
                          const payload = { detail: card.text } as CustomEventInit;
                          window.dispatchEvent(new CustomEvent("level:proactive_ask", payload));
                        }}
                      >
                        Yes, put it back
                      </button>
                      <button
                        type="button"
                        className="button-ghost"
                        onClick={() => void dismissProactiveCard(card.card_id)}
                      >
                        Not this week
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {hasMissing && (
            <section className={`${styles.block} ${styles.missing}`}>
              <div className={styles.missingHead}>
                <div>
                  <h2>Usuals missing this week</h2>
                  <p className={styles.missingHint}>
                    Only what&rsquo;s still coming up. Accident? Tell Level in the chat and it&rsquo;ll put them back. Resolved hides one; This week is different hides the set.
                  </p>
                </div>
                <button
                  className="button-ghost"
                  onClick={() => void dismissMissingWeek()}
                  title="Hide missing usuals until next week"
                >
                  This week is different
                </button>
              </div>
              <ul className={styles.missingWeek}>
                {missingByDay.map((day) => (
                  <li key={day.weekday} className={styles.missingDay}>
                    <span className={styles.missingDayLabel}>{WEEKDAY_LABEL[day.weekday]}</span>
                    <ul className={styles.missingList}>
                      {day.items.map((m) => {
                        const time =
                          m.typical_start && m.typical_end
                            ? `${m.typical_start}\u2013${m.typical_end}`
                            : "—";
                        const chips =
                          m.people?.length
                            ? m.people
                            : m.person_name
                              ? [
                                  {
                                    person_id: m.person_id,
                                    display_name: m.person_name,
                                    relation: m.person_relation,
                                  },
                                ]
                              : [];
                        return (
                          <li key={m.group_id} className={styles.missingRow}>
                            <span className={styles.missingTime}>{time}</span>
                            {chips.map((p) => {
                              const c = colorMap.get(p.person_id);
                              return (
                                <span
                                  key={p.person_id}
                                  className={styles.personChip}
                                  style={c ? { background: c.bg, borderColor: c.border, color: c.ink } : undefined}
                                >
                                  {p.display_name}
                                  {p.relation ? (
                                    <span className={styles.chipRel}>
                                      {" "}
                                      {RELATION_LABEL[p.relation] ?? p.relation}
                                    </span>
                                  ) : null}
                                </span>
                              );
                            })}
                            <span className={styles.missingCategory}>{m.category_label}</span>
                            <button
                              type="button"
                              className={styles.resolve}
                              onClick={() => void resolveMissingGroup(m.group_id)}
                              disabled={resolvingId === m.group_id}
                              title="Hide this missing usual until next week"
                            >
                              Resolved
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section className={styles.block}>
            <h2>Today</h2>
            {data?.today.length ? (
              <ul className={styles.list}>
                {data.today.map((e) => (
                  <li key={e.event_id}>
                    <EventCard event={e} />
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyCalendarHint
                when="today"
                sync={data?.sync}
              />
            )}
          </section>

          <section className={`${styles.block} ${styles.tomorrow}`}>
            <h2>Tomorrow</h2>
            {data?.tomorrow.length ? (
              <ul className={styles.list}>
                {data.tomorrow.map((e) => (
                  <li key={e.event_id}>
                    <EventCard event={e} />
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyCalendarHint when="tomorrow" sync={data?.sync} />
            )}
          </section>
        </div>

        <aside className={styles.rail} aria-label="Ask Level and reminders">
          <section className={styles.railBlock}>
            <Chat
              lead="Ask about your day, book a time, or draft an email &mdash; and Level will draft here."
              placeholder='"When&rsquo;s the best time to book team-dinner this week?" or "put back Tuesday Nova pickup"'
              busyHints={[
                "Looking at your calendar\u2026",
                "Weighing what this would crowd out\u2026",
                "Checking your care load\u2026",
                "Almost there\u2026",
              ]}
              onAfterReply={() => {
                setRemindersTick((n) => n + 1);
                void load();
              }}
            />
          </section>
          <section className={styles.railBlock}>
            <RemindersPanel key={remindersTick} onChange={load} />
          </section>
        </aside>
      </div>
    </div>
  );
}
