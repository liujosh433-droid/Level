/** Thin client for the Level FastAPI backend (cookie session). */

/**
 * Browser calls go same-origin (`/v1/...`) via Next rewrites so the session
 * cookie is first-party on :3000 — avoids localhost vs 127.0.0.1 cookie drops.
 * Server/SSR can still use an absolute URL.
 */
function resolveApiBase(): string {
  if (typeof window !== "undefined") {
    return "";
  }
  const fromEnv = process.env.NEXT_PUBLIC_LEVEL_API_URL?.replace(/\/$/, "");
  if (fromEnv) return fromEnv;
  return "http://127.0.0.1:8080";
}

/** Base URL for API calls and OAuth navigations. */
export function getApiBase(): string {
  return resolveApiBase();
}

export type ChallengeQuestion = {
  question: string;
  challenge_type: string;
  citations: { fact_id: string; quote: string; relevance: number }[];
};

export type Turn = {
  turn_id: string;
  status: string;
  user_text?: string | null;
  challenger_questions: ChallengeQuestion[];
  bias_event_ids?: string[];
  degradation_reason?: string | null;
};

export type Decision = {
  decision_id: string;
  user_id: string;
  status: string;
};

export type BiasScore = {
  category: string;
  ema: number;
  streak: number;
  total_observations: number;
};

export type ProfileBullet = {
  bullet_id: string;
  category: string;
  text: string;
  status: string;
  care_role_id?: string | null;
};

export type PendingChallenge = {
  decision_id: string;
  trigger_label: string;
  question?: string | null;
  challenge_type?: string | null;
};

export type CareGraphNode = {
  id: string;
  label: string;
  kind: string;
  hint?: string | null;
  role_id?: string | null;
  color?: string;
  event_count?: number;
  /** star = caregiver/helper; circle = dependent or domain load */
  shape?: string;
  /** AI-inferred relationship phrase (parent, child, …) */
  relationship?: string | null;
};

export type CareGraphEdge = {
  from_id: string;
  to_id: string;
  relation: string;
  role_id?: string | null;
  color?: string;
};

export type CareGraphCategory = {
  role_id: string;
  label: string;
  color: string;
  event_count: number;
};

export type CareGraph = {
  center: CareGraphNode;
  /** Caregiver roots (You + co-parents/helpers). Falls back to [center]. */
  roots?: CareGraphNode[];
  nodes: CareGraphNode[];
  edges: CareGraphEdge[];
  categories?: CareGraphCategory[];
};

export type HoldingChip = {
  label: string;
  role_id: string;
  color: string;
};

export type WeekRoleLoad = {
  role_id: string;
  label: string;
  color: string;
  percent: number;
  event_count?: number;
  minutes?: number;
};

export type Profile = {
  user_id: string;
  fact_count: number;
  manifesto: string | null;
  about_summary?: string | null;
  bias_scores: BiasScore[];
  session_count: number;
  needs_review?: boolean;
  bullets?: ProfileBullet[];
  contradictions?: { contradiction_id: string; summary: string; status: string }[];
  care_profile_version?: number | null;
  care_updated_at?: string | null;
  care_role_count?: number;
  conflict_summaries?: string[];
  care_graph?: CareGraph | null;
  people?: CarePersonView[];
};

export type CareContactView = {
  contact_id: string;
  role: string;
  name: string;
  email: string;
};

export type CarePersonView = {
  person_id: string;
  display_name: string;
  your_role: string;
  their_relation: string;
  care_role_id?: string;
  attendance_email: string;
  teacher_email: string;
  contacts?: CareContactView[];
};

export type Me = {
  user_id: string;
  email: string | null;
  display_name: string | null;
  google_connected: boolean;
  can_write_calendar?: boolean;
  can_send_email?: boolean;
};

export type TodayEvent = {
  id: string;
  summary: string;
  start: string | null;
  end: string | null;
  all_day: boolean;
  when_label: string;
  activity_kind: string;
  color: string;
  cues: string[];
};

export type TomorrowPreview = {
  weekday_label: string;
  date_label: string;
  summary: string;
  remember: string[];
  events: TodayEvent[];
};

export type TodayView = {
  user_id: string;
  display_name?: string | null;
  greeting_name: string;
  weekday_label: string;
  date_label: string;
  google_connected: boolean;
  events: TodayEvent[];
  recommendations: string[];
  tomorrow?: TomorrowPreview | null;
  profile_ready: boolean;
  needs_review: boolean;
  fact_count: number;
  manifesto: string | null;
  pending_challenges?: PendingChallenge[];
  care_graph?: CareGraph | null;
  holding?: HoldingChip[];
  week_load?: WeekRoleLoad[];
  conflict_summaries?: string[];
  usual_gaps?: UsualGapView[];
  proposed_usuals?: ProposedUsualView[];
};

export type UsualGapView = {
  usual_id: string;
  person_id: string;
  display_name: string;
  your_role: string;
  their_relation: string;
  label: string;
  on_date: string;
  weekday: number;
  start_minute: number;
  end_minute: number;
  banner: string;
};

export type ProposedUsualView = {
  usual_id: string;
  person_id: string;
  display_name: string;
  your_role: string;
  label: string;
  weekday: number;
  start_minute: number;
  end_minute: number;
};

export type CommitmentProposal = {
  proposal_id: string;
  user_id: string;
  kind: "add" | "availability" | "school_send";
  status: string;
  user_text: string;
  summary: string;
  level_message: string;
  recommended_action: string;
  conflicts: { summary: string; start?: string | null; end?: string | null; label: string }[];
  free_slots: { start: string; end: string; label: string }[];
  citations: { fact_id: string; quote: string }[];
  draft: {
    title: string;
    local_time: string;
    local_date?: string | null;
    duration_minutes: number;
    recurring: boolean;
    by_days: string[];
  };
  google_event_id?: string | null;
  to_email?: string;
  email_subject?: string;
  email_body?: string;
  cancel_event_ids?: string[];
};

export class AuthError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${resolveApiBase()}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
  });
  if (!res.ok) {
    const body = await res.text();
    if (res.status === 401) {
      throw new AuthError(401, body || "Not logged in");
    }
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return res.json() as Promise<T>;
}

/** Retry briefly — API --reload often returns 401 for a beat mid-restart. */
async function requestWithAuthRetry<T>(path: string, init?: RequestInit): Promise<T> {
  let lastErr: unknown;
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      return await request<T>(path, init);
    } catch (err) {
      lastErr = err;
      if (!(err instanceof AuthError) || attempt === 3) throw err;
      await sleep(250 * (attempt + 1));
    }
  }
  throw lastErr;
}

/** @deprecated identity is the httpOnly cookie — localStorage is not auth. */
export function getStoredUserId(): string {
  return "";
}

/** @deprecated no-op; session cookie is set by the API. */
export function storeUserId(_userId: string): void {
  // intentionally empty
}

export function clearLocalSession(): void {
  if (typeof window === "undefined") return;
  localStorage.removeItem("level_user_id");
}

export type GoogleSyncStatus = {
  google_connected: boolean;
  initial_sync_done: boolean;
  profile_ingested: boolean;
  agenda_event_count: number;
  watch_active: boolean;
  error: string | null;
};

export async function fetchMe(): Promise<Me> {
  if (typeof window !== "undefined") {
    const cached = readMeCache();
    if (cached) return cached;
    if (_meInflight) return _meInflight;
    _meInflight = requestWithAuthRetry<Me>("/v1/auth/me")
      .then((data) => {
        writeMeCache(data);
        return data;
      })
      .finally(() => {
        _meInflight = null;
      });
    return _meInflight;
  }
  return requestWithAuthRetry<Me>("/v1/auth/me");
}

const ME_CACHE_KEY = "level.me.v1";
const ME_CACHE_TTL_MS = 30_000;
let _meInflight: Promise<Me> | null = null;

function readMeCache(): Me | null {
  try {
    const raw = sessionStorage.getItem(ME_CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { at: number; data: Me };
    if (!parsed?.data || Date.now() - parsed.at > ME_CACHE_TTL_MS) return null;
    return parsed.data;
  } catch {
    return null;
  }
}

function writeMeCache(data: Me): void {
  try {
    sessionStorage.setItem(
      ME_CACHE_KEY,
      JSON.stringify({ at: Date.now(), data }),
    );
  } catch {
    /* ignore quota */
  }
}

export function invalidateMeCache(): void {
  _meInflight = null;
  if (typeof window === "undefined") return;
  try {
    sessionStorage.removeItem(ME_CACHE_KEY);
  } catch {
    /* ignore */
  }
}

export async function fetchGoogleSyncStatus(): Promise<GoogleSyncStatus> {
  return request<GoogleSyncStatus>("/v1/sources/google/status");
}

/** Resume cookie session or create a guest — always leaves you logged in. */
export async function ensureSession(displayName?: string): Promise<Me> {
  try {
    return await fetchMe();
  } catch (err) {
    if (err instanceof AuthError) {
      return createGuest(displayName);
    }
    throw err;
  }
}

export async function createGuest(displayName?: string): Promise<Me> {
  invalidateMeCache();
  const me = await request<Me>("/v1/auth/guest", {
    method: "POST",
    body: JSON.stringify(
      displayName ? { display_name: displayName } : {},
    ),
  });
  writeMeCache(me);
  return me;
}

export async function updateDisplayName(displayName: string): Promise<Me> {
  invalidateMeCache();
  const me = await request<Me>("/v1/auth/me", {
    method: "PATCH",
    body: JSON.stringify({ display_name: displayName }),
  });
  writeMeCache(me);
  return me;
}

export async function logout(): Promise<void> {
  try {
    await request<{ ok: boolean }>("/v1/auth/logout", {
      method: "POST",
      body: JSON.stringify({}),
    });
  } catch {
    // Still clear local prefs if the API is unreachable.
  }
  invalidateMeCache();
  clearLocalSession();
}

export async function createDecision(): Promise<Decision> {
  const data = await request<{ decision: Decision }>("/v1/decisions", {
    method: "POST",
    body: JSON.stringify({}),
  });
  return data.decision;
}

export async function takeTurn(decisionId: string, userText: string): Promise<Turn> {
  const data = await request<{ turn: Turn }>(`/v1/decisions/${decisionId}/turns`, {
    method: "POST",
    body: JSON.stringify({ user_text: userText }),
  });
  return data.turn;
}

export async function fetchProfile(): Promise<Profile> {
  return request<Profile>("/v1/sources/profile");
}

export async function fetchToday(): Promise<TodayView> {
  return request<TodayView>("/v1/today");
}

const TODAY_CACHE_KEY = "level.today.v1";
const TODAY_CACHE_TTL_MS = 60_000;

/** Instant paint from last successful Today response (≤60s), then revalidate. */
export function readTodayCache(): TodayView | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = sessionStorage.getItem(TODAY_CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { at: number; data: TodayView };
    if (!parsed?.data || Date.now() - parsed.at > TODAY_CACHE_TTL_MS) return null;
    return parsed.data;
  } catch {
    return null;
  }
}

export function writeTodayCache(data: TodayView): void {
  if (typeof window === "undefined") return;
  try {
    sessionStorage.setItem(
      TODAY_CACHE_KEY,
      JSON.stringify({ at: Date.now(), data }),
    );
  } catch {
    /* ignore quota */
  }
}

export async function dayCheckIn(
  message: string,
): Promise<{
  reply: string;
  facts_added: number;
  cues_added: number;
  today: TodayView | null;
  school_proposals?: CommitmentProposal[];
}> {
  return request(`/v1/today/check-in`, {
    method: "POST",
    body: JSON.stringify({ message }),
  });
}

export async function profileChat(
  message: string,
): Promise<{ reply: string; facts_added: number; profile: Profile }> {
  return request(`/v1/sources/profile/chat`, {
    method: "POST",
    body: JSON.stringify({ message }),
  });
}

export async function reviewProfile(
  bullets: { bullet_id: string; status: string; text?: string }[],
  markReviewed = true,
): Promise<Profile> {
  return request(`/v1/sources/profile/review`, {
    method: "POST",
    body: JSON.stringify({
      mark_reviewed: markReviewed,
      bullets,
    }),
  });
}

export async function proposeSchedule(
  text: string,
): Promise<{ is_schedule_ask: boolean; proposal: CommitmentProposal | null }> {
  return request(`/v1/calendar/propose`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}

export async function confirmProposal(
  proposalId: string,
  useSlotStart?: string | null,
): Promise<{ proposal: CommitmentProposal; google_event_id: string | null; html_link: string | null }> {
  return request(`/v1/calendar/proposals/${proposalId}/confirm`, {
    method: "POST",
    body: JSON.stringify({
      use_slot_start: useSlotStart ?? null,
    }),
  });
}

export async function resolveUsual(
  usualId: string,
  action: "put_back" | "exception" | "not_me" | "keep",
  onDate?: string | null,
): Promise<{ ok: boolean; google_event_id: string | null }> {
  return request(`/v1/care/usuals/resolve`, {
    method: "POST",
    body: JSON.stringify({
      usual_id: usualId,
      action,
      on_date: onDate ?? null,
    }),
  });
}

export async function savePersonContacts(
  personId: string,
  contacts: CareContactView[],
): Promise<{ ok: boolean; person_id?: string | null }> {
  return request(`/v1/care/people/contacts`, {
    method: "POST",
    body: JSON.stringify({ person_id: personId, contacts }),
  });
}

export async function addCarePerson(
  displayName: string,
  theirRelation = "",
  careRoleId = "child_care",
): Promise<{ ok: boolean; person_id?: string | null }> {
  return request(`/v1/care/people`, {
    method: "POST",
    body: JSON.stringify({
      display_name: displayName,
      their_relation: theirRelation,
      care_role_id: careRoleId,
    }),
  });
}

export async function ensureSelfPerson(
  displayName = "",
): Promise<{ ok: boolean; person_id?: string | null }> {
  return request(`/v1/care/people/self`, {
    method: "POST",
    body: JSON.stringify({ display_name: displayName }),
  });
}

export async function submitSchoolPaper(
  text: string,
  file?: File | null,
): Promise<{ proposal: CommitmentProposal | null; ask: string | null }> {
  const body = new FormData();
  if (text.trim()) body.append("text", text.trim());
  if (file) body.append("file", file);
  const res = await fetch(`${resolveApiBase()}/v1/care/school-paper`, {
    method: "POST",
    credentials: "include",
    body,
    cache: "no-store",
  });
  if (!res.ok) {
    const detail = await res.text();
    if (res.status === 401) {
      throw new AuthError(401, detail || "Not logged in");
    }
    throw new Error(`${res.status} ${res.statusText}: ${detail}`);
  }
  return res.json() as Promise<{
    proposal: CommitmentProposal | null;
    ask: string | null;
  }>;
}

export async function declineProposal(proposalId: string): Promise<CommitmentProposal> {
  return request(`/v1/calendar/proposals/${proposalId}/decline`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}
