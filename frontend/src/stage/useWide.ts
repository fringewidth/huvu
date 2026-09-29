import { useSyncExternalStore } from "react";

// Tailwind's `sm` breakpoint, for the few sizes that must be known in
// JavaScript rather than CSS (the answer crossfade scales between them).
const QUERY = "(min-width: 640px)";

function subscribe(onChange: () => void): () => void {
  const m = window.matchMedia(QUERY);
  m.addEventListener("change", onChange);
  return () => m.removeEventListener("change", onChange);
}

export function useWide(): boolean {
  return useSyncExternalStore(subscribe, () => window.matchMedia(QUERY).matches);
}
