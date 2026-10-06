import { useEffect, useState } from "react";

// The time, ticking every `ms` while `on`, for countdowns.
export function useNow(on: boolean, ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return;
    const tick = () => setNow(Date.now());
    // At once too, since the time kept while off is stale.
    const first = window.setTimeout(tick, 0);
    const timer = window.setInterval(tick, ms);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, [on, ms]);
  return now;
}

// What is left until `until`, as m:ss.
export function left(until: number, now: number): string {
  const seconds = Math.max(0, Math.ceil((until - now) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
