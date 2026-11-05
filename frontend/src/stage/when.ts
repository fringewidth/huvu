// When a turn happened, in the device's own locale and timezone, lower
// case like the rest of huvu's labels.

const TIME = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
const DATE = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short" });
const DATE_YEAR = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
const WEEKDAY = new Intl.DateTimeFormat(undefined, { weekday: "long" });

const DAY_MS = 24 * 60 * 60 * 1000;

export function timeOf(at: number): string {
  return TIME.format(at).toLowerCase();
}

// The calendar date, with the year only when it isn't this one.
export function dateOf(at: number, now = Date.now()): string {
  const format = new Date(at).getFullYear() === new Date(now).getFullYear() ? DATE : DATE_YEAR;
  return format.format(at).toLowerCase();
}

// The local calendar day, to tell whether two turns fall on the same one.
export function dayKey(at: number): string {
  const d = new Date(at);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

// The day as chat apps head it: today, yesterday, a weekday within the
// past week, and the date before that.
export function dayLabel(at: number, now = Date.now()): string {
  const midnight = (t: number) => new Date(t).setHours(0, 0, 0, 0);
  const days = Math.round((midnight(now) - midnight(at)) / DAY_MS);
  if (days === 0) return "today";
  if (days === 1) return "yesterday";
  if (days > 1 && days < 7) return WEEKDAY.format(at).toLowerCase();
  return dateOf(at, now);
}
