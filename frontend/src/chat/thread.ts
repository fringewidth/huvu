import { useSyncExternalStore } from "react";
import { followTurn, keptTurns, stopTurn, streamTurn, type Action, type KeptTurn } from "./backend";
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

// A notification huvu sent this phone (`notify` on the push stream, or
// `GET /api/notifications` for the ones it missed).
export interface Notice {
  id: string;
  urgency: "low" | "normal" | "high";
  // The app screen that deals with it, such as "triggers".
  opens: string | null;
  // Sent while the app was closed, and not shown since. It takes the
  // stage at launch the way a fresh turn does.
  unseen: boolean;
}

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
  // Set on a notification. huvu said it without being asked, so its ask
  // is empty and its answer is the text. The backend keeps it apart from
  // the conversation.
  notice: Notice | null;
  answer: string;
  // The model's reasoning summary, the current burst of it, while the
  // turn runs. The stage shows it faintly under the answer; it is never
  // kept, and a turn that has ended has none.
  thinking: string;
  parts: RichPart[];
  running: boolean;
  // Asked to stop, and waiting for the backend to say how it ended.
  stopping: boolean;
  ending: Ending;
}

export type Reply = Partial<Pick<Turn, "ask" | "answer" | "thinking" | "parts" | "ending">>;

export interface ThreadState {
  turns: Turn[];
  // A reply is still coming in. Its turn is the last one, or sits just
  // under feeds opened and notices sent since.
  running: boolean;
  stopping: boolean;
}

// A turn someone asked, as opposed to a watched feed or a notification.
export function asked(turn: Turn): boolean {
  return !turn.feed && !turn.notice;
}

let state: ThreadState = { turns: devSeed(), running: false, stopping: false };
const listeners = new Set<() => void>();
// The turns this client reads a stream of, each with its own. A new ask
// doesn't end the one before: the backend stops it, and its stream goes
// on until it says how it ended.
const streams = new Map<string, AbortController>();
// Turns the backend has heard of. A turn that never got that far (the
// backend was down) isn't there to replace.
const known = new Set<string>();
// Turns a rewind dropped. The backend's later events about them, and any
// listing that still has them, mean nothing here.
const dropped = new Set<string>();

// Turns get their id here and the backend keeps it, so a stop or a rewind
// can name a turn by an id this side already has.
function newTurnId(): string {
  return `t_${Array.from(crypto.getRandomValues(new Uint8Array(5)), (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

function blank(id: string, at: number): Turn {
  return { id, at, ask: "", feed: null, notice: null, answer: "", thinking: "", parts: [], running: false, stopping: false, ending: null };
}

function set(turns: Turn[]): void {
  const running = turns.filter((t) => t.running);
  state = { turns, running: running.length > 0, stopping: running.some((t) => t.stopping) };
  for (const listener of listeners) listener();
}

function patch(id: string, change: Partial<Turn>): void {
  const i = state.turns.findIndex((t) => t.id === id);
  if (i < 0) return;
  const turns = state.turns.slice();
  turns[i] = { ...turns[i], ...change };
  set(turns);
}

// Adds turns in time order, after any turn at the same moment.
function insert(turns: Turn[], more: Turn[]): Turn[] {
  const all = turns.slice();
  for (const turn of more) {
    const at = all.findIndex((t) => t.at > turn.at);
    all.splice(at < 0 ? all.length : at, 0, turn);
  }
  return all;
}

// The feeds opened since the last real turn. huvu isn't asked about them,
// but the next ask says they were opened, as context.
function actionsAfterLastAsk(turns: Turn[]): Action[] {
  let from = turns.length;
  while (from > 0 && !asked(turns[from - 1])) from--;
  return turns.slice(from).flatMap((t) => (t.feed ? [{ text: `opened the live feed ${t.feed.label}`, at: new Date(t.at).toISOString() }] : []));
}

// Reads one turn's stream into the turn, until the backend ends it.
async function read(id: string, replies: (signal: AbortSignal) => AsyncGenerator<Reply>): Promise<void> {
  const abort = new AbortController();
  streams.set(id, abort);
  try {
    for await (const reply of replies(abort.signal)) {
      known.add(id);
      patch(id, reply);
    }
    patch(id, { running: false, stopping: false, thinking: "" });
  } catch (err) {
    if (abort.signal.aborted) return;
    console.error("thread: reply failed", err);
    patch(id, { running: false, stopping: false, thinking: "", ending: "failed" });
  } finally {
    if (streams.get(id) === abort) streams.delete(id);
  }
}

// Follows a turn this client has no stream of: one it didn't start, or
// one running since before a restart. Unknown here, it is added first.
function follow(id: string, at = Date.now()): void {
  if (streams.has(id) || dropped.has(id)) return;
  if (!state.turns.some((t) => t.id === id)) set(insert(state.turns, [{ ...blank(id, at), running: true }]));
  else patch(id, { running: true });
  void read(id, (signal) => followTurn(id, signal));
}

// Drops the turns from `keep` on, asks `ask` in their place, and streams
// the answer into the new turn.
function answer(keep: number, ask: string): void {
  const kept = state.turns.slice(0, keep);
  const gone = state.turns.slice(keep);
  // The backend replaces from the first dropped turn it knows of.
  const replaces = gone.find((t) => known.has(t.id))?.id;
  for (const t of gone) {
    dropped.add(t.id);
    streams.get(t.id)?.abort();
  }
  const turn: Turn = { ...blank(newTurnId(), Date.now()), ask, running: true };
  set([...kept, turn]);
  void read(turn.id, (signal) => streamTurn({ id: turn.id, ask, replaces, actions: actionsAfterLastAsk(kept) }, signal));
}

function indexOf(id: string): number {
  const i = state.turns.findIndex((t) => t.id === id);
  if (i < 0) throw new Error(`thread: no turn ${id}`);
  return i;
}

export function ask(text: string): void {
  answer(state.turns.length, text);
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
  set([...state.turns, { ...blank(id, Date.now()), feed, answer: feed.label, parts: [part] }]);
}

// Rewind: the turn and everything after it are replaced by a new ask.
export function edit(id: string, text: string): void {
  answer(indexOf(id), text);
}

// Asks the same thing again. Turns after it are dropped.
export function retry(id: string): void {
  const i = indexOf(id);
  answer(i, state.turns[i].ask);
}

// Asks the backend to stop every reply still coming in. Each keeps
// streaming until the backend ends it, and its `turn.ended` says whether
// Hermes stopped or finished anyway.
export function stop(): void {
  for (const turn of state.turns) {
    if (!turn.running || turn.stopping) continue;
    patch(turn.id, { stopping: true });
    void stopTurn(turn.id);
  }
}

// The push stream carries this device's turn events too. A turn this
// client streams already, or one that is over, needs nothing. Any other
// (an ask that reached this device some other way, or one running since
// before the stream dropped) is followed from its start.
export function heard(id: string): void {
  const mine = state.turns.find((t) => t.id === id);
  if (streams.has(id) || dropped.has(id) || (mine && !mine.running)) return;
  follow(id);
}

export interface NoticeData {
  id: string;
  at?: string;
  text: string;
  image?: string | null;
  urgency?: string;
  opens?: string | null;
}

// Adds notifications as turns of their own, once each. Those from before
// `seenUntil` were shown already; later ones from before this launch take
// the stage the way a fresh turn would.
export function notices(sent: NoticeData[], seenUntil: number | null): void {
  const have = new Set(state.turns.map((t) => t.id));
  const now = Date.now();
  const more = sent
    .filter((n) => !have.has(n.id) && !dropped.has(n.id))
    .map((n): Turn => {
      const at = n.at ? Date.parse(n.at) : now;
      const urgency = n.urgency === "low" || n.urgency === "high" ? n.urgency : "normal";
      const notice: Notice = { id: n.id, urgency, opens: n.opens ?? null, unseen: n.at !== undefined && seenUntil !== null && at > seenUntil };
      return { ...blank(n.id, n.at ? at : now), notice, answer: n.text, parts: n.image ? [{ kind: "image", src: n.image }] : [] };
    });
  if (more.length === 0) return;
  // A reply still coming in keeps the stage. The notice goes in under it,
  // and waits in history.
  const reply = state.turns.findIndex((t) => t.running && asked(t));
  set(reply < 0 ? insert(state.turns, more) : [...insert(state.turns.slice(0, reply), more), ...state.turns.slice(reply)]);
}

// One notification, as the push stream brings it: it is shown now, so its
// time is this moment.
export function notify(n: NoticeData): void {
  notices([{ ...n, at: undefined }], null);
}

// Catches up with the backend: at launch, and each time the push stream
// connects again. Turns this client doesn't have are added, one that ended
// while nobody here listened takes the backend's ending, and one still
// running is followed from its start.
export async function sync(): Promise<void> {
  let kept: KeptTurn[];
  try {
    kept = await keptTurns();
  } catch (err) {
    console.error("thread: could not load the conversation", err);
    return;
  }
  const local = new Map(state.turns.map((t) => [t.id, t]));
  const added: Turn[] = [];
  let turns = state.turns;
  for (const k of kept) {
    known.add(k.id);
    const mine = local.get(k.id);
    if (dropped.has(k.id) || streams.has(k.id)) continue;
    const turn: Turn = { ...blank(k.id, Date.parse(k.at)), ask: k.ask, answer: k.answer.trim(), parts: k.parts, running: k.running, ending: k.ending };
    if (!mine) added.push(turn);
    else if (mine.running && !k.running) turns = turns.map((t) => (t.id === k.id ? turn : t));
  }
  set(insert(turns, added));
  for (const k of kept) if (k.running) follow(k.id);
}

// Forgets everything: the device logged out, and the next one to log in
// may be someone else.
export function reset(): void {
  for (const abort of streams.values()) abort.abort();
  streams.clear();
  known.clear();
  dropped.clear();
  set([]);
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
