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

const ACTIVITY_COLOR: Record<string, string> = {
  "sports.soccer": "#3aa38a",
  "sports.basketball": "#c4843a",
  "sports.swim": "#3a95c4",
  "sports.other": "#3aa38a",
  "school.pickup": "#c4843a",
  "school.dropoff": "#c4843a",
  "school.event": "#c4843a",
  "medical.appointment": "#c44d4d",
  "medical.therapy": "#a06ac4",
  "work": "#5a7380",
  "family": "#c47a3a",
  "commute": "#8aa4b0",
  "personal": "#2d9f8a",
  "other": "#8aa4b0",
};

export function activityEmoji(activityType?: string | null): string {
  if (!activityType) return "📅";
  return ACTIVITY_EMOJI[activityType] ?? "📅";
}

export function activityColor(activityType?: string | null): string {
  if (!activityType) return "#8aa4b0";
  return ACTIVITY_COLOR[activityType] ?? "#8aa4b0";
}

export function activityLabel(activityType?: string | null): string {
  if (!activityType) return "Event";
  const [head, tail] = activityType.split(".");
  const nice = (tail ?? head).replace(/_/g, " ");
  return nice.charAt(0).toUpperCase() + nice.slice(1);
}
