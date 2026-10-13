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

// Whether the wiki is open over the stage. The menu opens it, and the
// stage's back closes it.
let open = false;
const listeners = new Set<() => void>();

export function setWikiOpen(next: boolean): void {
  if (open === next) return;
  open = next;
  for (const l of listeners) l();
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

// What back does inside the wiki: the open view walks out one step, and
// says false once it is at the contents, where back closes the wiki.
let back: (() => boolean) | null = null;

export function setWikiBack(handler: (() => boolean) | null): void {
  back = handler;
}

// Back while the wiki is open: one step out, then out of the wiki.
export function wikiBack(): void {
  if (!back?.()) setWikiOpen(false);
}
