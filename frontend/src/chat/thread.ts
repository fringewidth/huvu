import { useSyncExternalStore } from "react";
import { streamReply, type HistoryMessage } from "./backend";
import { devSeed } from "./devSeed";

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
  answer: string;
  parts: RichPart[];
  running: boolean;
  ending: Ending;
}

export type Reply = Partial<Pick<Turn, "answer" | "parts">>;

export interface ThreadState {
  turns: Turn[];
  // The last turn is still being answered.
  running: boolean;
}

let state: ThreadState = { turns: devSeed(), running: false };
const listeners = new Set<() => void>();
let run: { id: string; abort: AbortController } | null = null;
let nextId = 0;

function set(turns: Turn[]): void {
  state = { turns, running: turns.at(-1)?.running ?? false };
  for (const listener of listeners) listener();
}

function patch(id: string, change: Partial<Turn>): void {
  const i = state.turns.findIndex((t) => t.id === id);
  if (i < 0) return;
  const turns = state.turns.slice();
  turns[i] = { ...turns[i], ...change };
  set(turns);
}

function history(turns: Turn[], ask: string): HistoryMessage[] {
  const past = turns.flatMap((t): HistoryMessage[] => [
    { role: "user", text: t.ask },
    ...(t.answer ? [{ role: "assistant" as const, text: t.answer }] : []),
  ]);
  return [...past, { role: "user", text: ask }];
}

// Drops the turns from `keep` on, asks `ask` in their place, and streams
// the answer into the new turn.
async function answer(keep: number, ask: string): Promise<void> {
  stop();
  const kept = state.turns.slice(0, keep);
  const turn: Turn = { id: `turn-${++nextId}`, at: Date.now(), ask, answer: "", parts: [], running: true, ending: null };
  set([...kept, turn]);
  const abort = new AbortController();
  run = { id: turn.id, abort };
  try {
    for await (const reply of streamReply(history(kept, ask), abort.signal)) patch(turn.id, reply);
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
