const ACTIVITY_EMOJI: Record<string, string> = {
  "sports.soccer": "⚽",
  "sports.basketball": "🏀",
  "sports.swim": "🏊",
  "sports.other": "🏃",
  "school.pickup": "🎒",
  "school.dropoff": "🚸",
  "school.event": "🏫",
  "medical.appointment": "🩺",
  "medical.therapy": "🧠",
  "work": "💼",
  "family": "👨‍👩‍👧",
  "commute": "🚗",
  "personal": "🌿",
  "other": "📅",
};

// Kept in sync with LoadBucket colors in
// packages/core/src/level_core/schemas/activity.py so the calendar
// event chips and the weekly held-load bar tell the same visual
// story. Prior palette had WORK, COMMUTE, and OTHER all in the same
// gray-blue family which made it impossible to tell them apart.
const ACTIVITY_COLOR: Record<string, string> = {
  "sports.soccer": "#3aa38a",     // green-teal
  "sports.basketball": "#3aa38a", // green-teal (unified w/ bucket)
  "sports.swim": "#3aa38a",       // green-teal (unified w/ bucket)
  "sports.other": "#3aa38a",      // green-teal
  "school.pickup": "#d99a4a",     // amber
  "school.dropoff": "#d99a4a",    // amber
  "school.event": "#d99a4a",      // amber
  "medical.appointment": "#d15b5b", // rose
  "medical.therapy": "#d15b5b",     // rose (unified w/ bucket)
  "work": "#5b7cd8",              // indigo blue (was gray-blue #5a7380)
  "family": "#e07a5b",            // warm coral
  "commute": "#9464c8",           // violet (was gray-blue #8aa4b0)
  "personal": "#c88b9a",          // dusty pink (was teal, clashed w/ sports)
  "other": "#8892a6",             // muted slate (distinct from commute now)
};

const FALLBACK_COLOR = "#8892a6"; // muted slate; matches OTHER bucket

export function activityEmoji(activityType?: string | null): string {
  if (!activityType) return "📅";
  return ACTIVITY_EMOJI[activityType] ?? "📅";
}

export function activityColor(activityType?: string | null): string {
  if (!activityType) return FALLBACK_COLOR;
  return ACTIVITY_COLOR[activityType] ?? FALLBACK_COLOR;
}

export function activityLabel(activityType?: string | null): string {
  if (!activityType) return "Event";
  const [head, tail] = activityType.split(".");
  const nice = (tail ?? head).replace(/_/g, " ");
  return nice.charAt(0).toUpperCase() + nice.slice(1);
}
