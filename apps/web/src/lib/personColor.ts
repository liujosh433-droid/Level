// Calm accent palette, 8 hues spaced ~45° around the wheel.
// Small enough to feel curated, wide enough that no two adjacent slots
// read as the same colour family (no teal/moss doubles).
export const PALETTE = [
  { bg: "rgba(214, 108, 91, 0.16)",  border: "rgba(214, 108, 91, 0.55)",  ink: "#a04030" }, // coral    10°
  { bg: "rgba(217, 148, 46, 0.18)",  border: "rgba(217, 148, 46, 0.55)",  ink: "#9d5c1c" }, // amber    35°
  { bg: "rgba(150, 168, 66, 0.20)",  border: "rgba(150, 168, 66, 0.55)",  ink: "#5c6a1e" }, // olive    65°
  { bg: "rgba(77, 143, 94, 0.20)",   border: "rgba(77, 143, 94, 0.55)",   ink: "#345a3f" }, // moss    135°
  { bg: "rgba(45, 159, 138, 0.16)",  border: "rgba(45, 159, 138, 0.55)",  ink: "#1e6b5c" }, // teal    170°
  { bg: "rgba(70, 119, 203, 0.16)",  border: "rgba(70, 119, 203, 0.55)",  ink: "#2f4f9c" }, // ocean   215°
  { bg: "rgba(113, 89, 200, 0.16)",  border: "rgba(113, 89, 200, 0.55)",  ink: "#4d3aa0" }, // indigo  255°
  { bg: "rgba(200, 87, 136, 0.16)",  border: "rgba(200, 87, 136, 0.55)",  ink: "#8a3468" }, // rose    335°
] as const;

const NEUTRAL = { bg: "rgba(90, 115, 128, 0.10)", border: "rgba(90, 115, 128, 0.35)", ink: "#556974" };

export type PersonColor = { bg: string; border: string; ink: string };

/**
 * Assign a colour to each key in `keys`. Ordered assignment (by sorted key)
 * guarantees the first PALETTE.length distinct keys land on distinct hues —
 * no more Nova/Theo collisions.
 */
export function buildPersonColorMap(keys: readonly string[]): Map<string, PersonColor> {
  const unique = Array.from(new Set(keys.filter(Boolean))).sort();
  const map = new Map<string, PersonColor>();
  unique.forEach((k, i) => {
    map.set(k, PALETTE[i % PALETTE.length]);
  });
  return map;
}

/** Fallback for callers that don't have the full people list. */
export function personColor(
  key: string | null | undefined,
  map?: Map<string, PersonColor>,
): PersonColor {
  if (!key) return NEUTRAL;
  return map?.get(key) ?? NEUTRAL;
}
