/** Thin client for the Level FastAPI backend (cookie session). */

const API_BASE = process.env.NEXT_PUBLIC_LEVEL_API_URL ?? "http://localhost:8080";

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
};

export type Profile = {
  user_id: string;
  fact_count: number;
  manifesto: string | null;
  bias_scores: BiasScore[];
  session_count: number;
  needs_review?: boolean;
  bullets?: ProfileBullet[];
  contradictions?: { contradiction_id: string; summary: string; status: string }[];
};

export type Me = {
  user_id: string;
  email: string | null;
  display_name: string | null;
  google_connected: boolean;
};

export type TodayEvent = {
  id: string;
  summary: string;
  start: string | null;
  end: string | null;
  all_day: boolean;
  when_label: string;
};

export type TodayView = {
  user_id: string;
  google_connected: boolean;
  events: TodayEvent[];
  recommendations: string[];
  profile_ready: boolean;
  needs_review: boolean;
  fact_count: number;
  manifesto: string | null;
};

export type CommitmentProposal = {
  proposal_id: string;
  user_id: string;
  kind: "add" | "availability";
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
};

export class AuthError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
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
  localStorage.removeItem("level_include_drive");
}

export async function fetchMe(): Promise<Me> {
  return request<Me>("/v1/auth/me");
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
  return request<Me>("/v1/auth/guest", {
    method: "POST",
    body: JSON.stringify({ display_name: displayName ?? "Guest parent" }),
  });
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

export async function declineProposal(proposalId: string): Promise<CommitmentProposal> {
  return request(`/v1/calendar/proposals/${proposalId}/decline`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export { API_BASE };
