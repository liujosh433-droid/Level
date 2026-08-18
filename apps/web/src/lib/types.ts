export type WhoAmI = {
  user_id: string;
  email: string | null;
  google_connected: boolean;
  tz: string | null;
};

export type CarePerson = {
  person_id: string;
  display_name: string;
  relation: string;
  care_role_id: string;
  aliases: string[];
  is_self: boolean;
  status: "proposed" | "kept" | "not_me";
};

export type Usual = {
  usual_id: string;
  person_id: string;
  weekday: number;
  hour_band: string;
  activity_type: string;
  display_summary: string;
  confidence: number;
  status: "proposed" | "kept" | "not_me";
};

export type Priority = {
  priority_id: string;
  text: string;
  weight: number;
  activity_types: string[];
  status: "kept" | "not_me";
};

export type Reminder = {
  reminder_id: string;
  text: string;
  match: { person_id: string | null; activity_type: string };
  status: "active" | "dismissed";
};

export type Contact = {
  contact_id: string;
  person_id: string;
  kind: "teacher" | "doctor" | "coach" | "other";
  name: string;
  email?: string | null;
  phone?: string | null;
  notes?: string;
};

export type TodayEvent = {
  event_id: string;
  summary: string;
  start: string;
  end: string;
  activity_type: string | null;
  origin: "google" | "level";
  level_reason: string | null;
  people: { person_id: string; display_name: string | null }[];
  reminders: { reminder_id: string; text: string }[];
};

export type WeekLoadRow = {
  activity_type: string;
  label: string;
  color: string;
  count: number;
  percent: number;
};

export type TodayResponse = {
  date: string;
  today: TodayEvent[];
  tomorrow: TodayEvent[];
  missing_usuals: {
    usual_id: string;
    display_summary: string;
    person_id: string;
    hour_band: string;
  }[];
  week_load: WeekLoadRow[];
};

export type SourcesStatus = {
  google_connected: boolean;
  email: string | null;
  calendar_id: string | null;
  last_pull_at: string | null;
  days_back: number | null;
  days_forward: number | null;
  watch: unknown;
  ai_calls_total: number;
};

export type TraceEntry = {
  audit_id: string;
  agent: string;
  model: string;
  cost_estimate_usd: number;
  latency_ms: number;
  hallucinated: boolean;
  blocked_by_safety: boolean;
  created_at: string;
};
