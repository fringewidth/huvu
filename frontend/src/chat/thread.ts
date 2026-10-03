import { useSyncExternalStore } from "react";
import { streamTurn, stopTurn, type Action } from "./backend";
import { devSeed } from "./devSeed";
import type { Feed } from "./feeds";

// The one conversation, held as turns: one ask and what huvu did with it.
// The stage shows turns, never a chat log, so this is the model itself
// and not something derived from messages. Every change replaces only the
// turn it touches, so a streaming reply re-renders only its own turn.
export type RichPart =
  | { kind: "image"; src: string }
  | { kind: "tool"; id: string; toolName: string; args: Record<string, unknown>; result: unknown };
export type ToolPart = Extract<RichPart, { kind: "tool" }>;
export type ImagePart = Extract<RichPart, { kind: "image" }>;

// How huvu's reply ended, when it didn't simply finish.
export type Ending = "failed" | "stopped" | null;

export interface Turn {
  id: string;
  // When the ask was made. Turns older than this session (seeded or
  // restored) never grow the garland or get announced.
  at: number;
  ask: string;
  // Set on a turn the user opened to watch a live feed. Such a turn asks
  // nothing and huvu never answers it: its ask is empty, its answer is
  // the feed's name, and its only part is the feed. It makes no call of
  // its own. The next ask carries it to the backend as an action.
  feed: Feed | null;
  answer: string;
  parts: RichPart[];
  running: boolean;
  ending: Ending;
}

export type Reply = Partial<Pick<Turn, "answer" | "parts" | "ending">>;

export interface ThreadState {
  turns: Turn[];
  // A reply is still coming in. Its turn is the last one, or sits just
  // under feeds opened since.
  running: boolean;
}

let state: ThreadState = { turns: devSeed(), running: false };
const listeners = new Set<() => void>();
let run: { id: string; abort: AbortController } | null = null;
// Turns the backend has heard of. A turn that never got that far (the
// backend was down) isn't there to replace.
const known = new Set<string>();

// Turns get their id here and the backend keeps it, so a stop or a rewind
// can name a turn by an id this side already has.
function newTurnId(): string {
  return `t_${Array.from(crypto.getRandomValues(new Uint8Array(5)), (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

function set(turns: Turn[]): void {
  state = { turns, running: turns.some((t) => t.running) };
  for (const listener of listeners) listener();
}

function patch(id: string, change: Partial<Turn>): void {
  const i = state.turns.findIndex((t) => t.id === id);
  if (i < 0) return;
  const turns = state.turns.slice();
  turns[i] = { ...turns[i], ...change };
  set(turns);
}

// The feeds opened since the last real turn. huvu isn't asked about them,
// but the next ask says they were opened, as context.
function actionsAfterLastAsk(turns: Turn[]): Action[] {
  let from = turns.length;
  while (from > 0 && turns[from - 1].feed) from--;
  return turns.slice(from).flatMap((t) => (t.feed ? [{ text: `opened the live feed ${t.feed.label}`, at: new Date(t.at).toISOString() }] : []));
}

// Drops the turns from `keep` on, asks `ask` in their place, and streams
// the answer into the new turn.
async function answer(keep: number, ask: string): Promise<void> {
  stop();
  const kept = state.turns.slice(0, keep);
  // The backend replaces from the first dropped turn it knows of.
  const replaces = state.turns.slice(keep).find((t) => known.has(t.id))?.id;
  const turn: Turn = { id: newTurnId(), at: Date.now(), ask, feed: null, answer: "", parts: [], running: true, ending: null };
  set([...kept, turn]);
  const abort = new AbortController();
  run = { id: turn.id, abort };
  try {
    for await (const reply of streamTurn({ id: turn.id, ask, replaces, actions: actionsAfterLastAsk(kept) }, abort.signal)) {
      known.add(turn.id);
      patch(turn.id, reply);
    }
    patch(turn.id, { running: false });
  } catch (err) {
    // stop() has already marked a cancelled turn.
    if (abort.signal.aborted) return;
    console.error("thread: reply failed", err);
    patch(turn.id, { running: false, ending: "failed" });
  } finally {
    if (run?.abort === abort) run = null;
  }
}

function indexOf(id: string): number {
  const i = state.turns.findIndex((t) => t.id === id);
  if (i < 0) throw new Error(`thread: no turn ${id}`);
  return i;
}

export function ask(text: string): void {
  void answer(state.turns.length, text);
}

// Opens a live feed as a turn of its own, with no call to the backend. A
// reply still coming in keeps streaming into its own turn underneath.
export function watch(feed: Feed): void {
  const id = newTurnId();
  const part: RichPart = {
    kind: "tool",
    id: `${id}-feed`,
    toolName: "video_feed",
    // The turn's answer already names the feed, so its caption says what
    // kind of feed it is.
    args: { src: feed.src, label: feed.kind },
    result: {},
  };
  set([...state.turns, { id, at: Date.now(), ask: "", feed, answer: feed.label, parts: [part], running: false, ending: null }]);
}

// Rewind: the turn and everything after it are replaced by a new ask.
export function edit(id: string, text: string): void {
  void answer(indexOf(id), text);
}

// Asks the same thing again. Turns after it are dropped.
export function retry(id: string): void {
  const i = indexOf(id);
  void answer(i, state.turns[i].ask);
}

export function stop(): void {
  if (!run) return;
  const { id, abort } = run;
  run = null;
  abort.abort();
  void stopTurn(id);
  patch(id, { running: false, ending: "stopped" });
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getThread(): ThreadState {
  return state;
}

export function useThread(): ThreadState {
  return useSyncExternalStore(subscribe, getThread);
}
