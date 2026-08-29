/** Format a `YYYY-MM-DD` calendar date in the user's timezone.

`new Date("2026-08-24")` is UTC midnight, which is still Sunday evening in
US zones — that is why prod said Happy Sunday on Monday.

The API already emits dates in the user's IANA zone (`data.tz`). We
build a local noon on that calendar day so a `timeZone` override
cannot roll the date in typical US offsets.
*/
export function formatDateOnly(isoDate: string, opts: Intl.DateTimeFormatOptions): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (!match) return isoDate;
  const date = new Date(
    Number(match[1]),
    Number(match[2]) - 1,
    Number(match[3]),
    12,
    0,
    0,
  );
  return date.toLocaleDateString([], opts);
}

export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "America/Los_Angeles";
  } catch {
    return "America/Los_Angeles";
  }
}
