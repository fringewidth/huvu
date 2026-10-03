import { useSyncExternalStore } from "react";
import { BACKEND } from "./backend";

// Every live feed the gateway could stream right now: cameras, huvu's
// browser, a screen. Listing a feed opens nothing. A feed streams only
// once huvu or the user picks it, and only while the stage's turn shows
// it (see VideoFeed in stage/partViews.tsx).
export interface Feed {
  id: string;
  label: string;
  kind: string;
  // An offline feed stays listed, so huvu can say the camera is down.
  online: boolean;
  // The feed's WHEP endpoint.
  src: string;
}

const FEEDS_ENDPOINT = `${BACKEND}/api/feeds`;

let feeds: Feed[] = [];
const listeners = new Set<() => void>();
let loading: Promise<void> | null = null;

// Fetches the list again: at boot, and each time the list opens, so a
// feed that came or went since shows up. A failed fetch keeps the last
// list.
export function refreshFeeds(): Promise<void> {
  loading ??= (async () => {
    try {
      const res = await fetch(FEEDS_ENDPOINT);
      if (!res.ok) throw new Error(`feeds: backend answered ${res.status}`);
      feeds = parse(await res.json());
      for (const listener of listeners) listener();
    } catch (err) {
      console.error("feeds: could not load the list", err);
    } finally {
      loading = null;
    }
  })();
  return loading;
}

function parse(raw: unknown): Feed[] {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((f): Feed[] => {
    if (typeof f?.id !== "string" || typeof f.src !== "string") return [];
    return [
      {
        id: f.id,
        label: typeof f.label === "string" ? f.label : f.id,
        kind: typeof f.kind === "string" ? f.kind : "feed",
        online: f.online !== false,
        src: f.src,
      },
    ];
  });
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useFeeds(): Feed[] {
  return useSyncExternalStore(subscribe, () => feeds);
}
