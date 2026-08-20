export type WhoAmI = {
  user_id: string;
  email: string | null;
  display_name?: string | null;
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
  typical_start?: string | null;
  typical_end?: string | null;
  person_name?: string | null;
  person_relation?: string | null;
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
  bucket: string;
  label: string;
  color: string;
  count: number;
  percent: number;
};

export type MissingUsualPerson = {
  person_id: string;
  display_name: string | null;
  relation: string | null;
};

export type MissingUsualWeek = {
  group_id: string;
  weekday: number;
  date: string;
  category: string;
  category_label: string;
  person_id: string;
  person_name: string | null;
  person_relation: string | null;
  people?: MissingUsualPerson[];
  typical_start: string | null;
  typical_end: string | null;
};

export type CalendarSyncInfo = {
  calendars: { id: string; summary?: string | null; primary?: boolean }[];
  last_error?: string | null;
  last_pull_at?: string | null;
  total_cached: number;
  pulling?: boolean;
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
  missing_usuals_week: MissingUsualWeek[];
  missing_usuals_week_dismissed?: boolean;
  week_load: WeekLoadRow[];
  sync?: CalendarSyncInfo;
};

export type SourcesStatus = {
  google_connected: boolean;
  email: string | null;
  calendar_id: string | null;
  calendars?: { id: string; summary?: string | null; primary?: boolean }[];
  last_error?: string | null;
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
