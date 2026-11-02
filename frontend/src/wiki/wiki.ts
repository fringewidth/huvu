import { useSyncExternalStore } from "react";
import { call, getJson } from "../net/http";

// huvu's wiki of its person: Markdown pages the agent writes and keeps in
// order, its memory. The person reads it here and can correct it. The
// agent owns it, so the app never reorganizes it: it shows the pages and
// writes back only what the person edits.

export interface WikiPageInfo {
  path: string;
  title: string;
  modified: string;
  size: number;
}

export interface WikiListing {
  pages: WikiPageInfo[];
  // MEMORY.md's entries, the agent's index of its wiki, in its order.
  index: string[];
}

export interface WikiPage {
  path: string;
  text: string;
  // A hash of the text. A save names the version it was made from, so it
  // never lands over a change huvu made meanwhile.
  version: string;
  modified: string;
}

export function listWiki(): Promise<WikiListing> {
  return getJson<WikiListing>("/api/wiki");
}

export function readPage(path: string): Promise<WikiPage> {
  return getJson<WikiPage>(`/api/wiki/page?path=${encodeURIComponent(path)}`);
}

// Answers 409 when huvu changed the page since `version`.
export async function savePage(path: string, text: string, version: string | null): Promise<WikiPage> {
  return (await call("/api/wiki/page", { method: "PUT", json: { path, text, version } })).json() as Promise<WikiPage>;
}

export async function deletePage(path: string, version: string): Promise<void> {
  await call(`/api/wiki/page?path=${encodeURIComponent(path)}&version=${encodeURIComponent(version)}`, { method: "DELETE" });
}

// Whether the wiki is open over the stage. The menu opens it, and back
// closes it.
//
// Each step in the wiki is an entry in the browser's history: opening it,
// and each page or edit after that. So the browser's back walks out of it
// one step at a time, and every other back (Escape, Android's back button,
// the close button) goes through the history too, so the two never
// disagree. An entry's state names its depth and the opening it belongs
// to, so an entry left from an earlier opening, or from before a reload,
// is never taken for a step of this one.
let open = false;
let depth = -1;
let opening = "";
const listeners = new Set<() => void>();
const steps = new Set<(depth: number) => void>();

interface Entry {
  huvuWiki: number;
  opening: string;
}

function entryOf(state: unknown): Entry | null {
  const e = state as Partial<Entry> | null;
  return e && typeof e.huvuWiki === "number" && e.opening === opening ? (e as Entry) : null;
}

function changed(next: boolean): void {
  open = next;
  for (const l of listeners) l();
}

window.addEventListener("popstate", (e) => {
  if (!open) return;
  const entry = entryOf(e.state);
  if (!entry) {
    depth = -1;
    changed(false);
    return;
  }
  depth = entry.huvuWiki;
  for (const s of steps) s(depth);
});

export function openWiki(): void {
  if (open) return;
  opening = Math.random().toString(36).slice(2);
  depth = 0;
  window.history.pushState({ huvuWiki: 0, opening } satisfies Entry, "");
  changed(true);
}

// Closes the wiki by going back past every entry it made.
export function closeWiki(): void {
  if (open) window.history.go(-(depth + 1));
}

// One step out: a page to where it was opened from, the contents out of
// the wiki.
export function wikiBack(): void {
  if (open) window.history.back();
}

// A step further in, as a new entry. Returns its depth.
export function wikiForward(): number {
  depth += 1;
  window.history.pushState({ huvuWiki: depth, opening } satisfies Entry, "");
  return depth;
}

// Back to the entry at `to`, the contents being 0.
export function wikiBackTo(to: number): void {
  if (open && to < depth) window.history.go(to - depth);
}

export function wikiDepth(): number {
  return depth;
}

// Calls `listener` with the depth the history moved the wiki to.
export function onWikiStep(listener: (depth: number) => void): () => void {
  steps.add(listener);
  return () => steps.delete(listener);
}

// Calls `listener` each time the wiki opens or closes.
export function onWikiChange(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useWikiOpen(): boolean {
  return useSyncExternalStore(onWikiChange, () => open);
}

export function isWikiOpen(): boolean {
  return open;
}
