import { useSyncExternalStore } from "react";
import { followTurn, keptTurns, stopTurn, streamTurn, type Action, type KeptTurn } from "./backend";
import { devSeed } from "./devSeed";
import type { Feed } from "./feeds";

// The conversation, held as turns: one ask and what huvu did with it.
// The stage shows turns, never a chat log, so this is the model itself
// and not something derived from messages. Every change replaces only the
// turn it touches, so a streaming reply re-renders only its own turn.
// `after` is how much of the answer huvu had said when the part came, so a
// note sits between the words around it. Parts without one go first.
//
// There is one store per conversation: the device's own, and the wake on
// the stage, when one is open (see wakes.ts). A wake is a model instance a
// trigger started; it has its own turns, the first with no ask, and no
// edit or retry. What this module exports acts on the conversation on the
// stage, so the stage, the voice and the spoken answers follow a wake the
// way they follow the device's own.
export type RichPart =
  | { kind: "image"; src: string; caption?: string; after?: number }
  | { kind: "tool"; id: string; toolName: string; args: Record<string, unknown>; result: unknown; after?: number };
export type ToolPart = Extract<RichPart, { kind: "tool" }>;
export type ImagePart = Extract<RichPart, { kind: "image" }>;

// How huvu's reply ended, when it didn't simply finish.
export type Ending = "failed" | "stopped" | null;

// A notification huvu sent this phone (`notify` on the push stream, or
// `GET /api/notifications` for the ones it missed).
export interface Notice {
  id: string;
  urgency: "low" | "normal" | "high";
  // The app screen that deals with it: "triggers", or "wake:<id>" for the
  // wake that sent it.
  opens: string | null;
  // Who sent it through huvu, as this user calls them, such as "mom".
  // Null when huvu sent it.
  from: string | null;
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
  // The wake this is, or null for the device's own conversation.
  wake: string | null;
}

// A turn someone asked, as opposed to a watched feed or a notification.
// A wake's first turn, which its trigger started, counts as asked: huvu
// answered something.
export function asked(turn: Turn): boolean {
  return !turn.feed && !turn.notice;
}

interface Store {
  state: ThreadState;
  // The turns this client reads a stream of, each with its own. A new ask
  // doesn't end the one before: the backend stops it, and its stream goes
  // on until it says how it ended.
  streams: Map<string, AbortController>;
  // Turns the backend has heard of. A turn that never got that far (the
  // backend was down) isn't there to replace.
  known: Set<string>;
  // Turns a rewind dropped. The backend's later events about them, and
  // any listing that still has them, mean nothing here.
  dropped: Set<string>;
}

function newStore(wake: string | null, turns: Turn[] = []): Store {
  return { state: { turns, running: false, stopping: false, wake }, streams: new Map(), known: new Set(), dropped: new Set() };
}

const own = newStore(null, devSeed());
let wakeStore: Store | null = null;
// The conversation on the stage.
let active = own;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

// Turns get their id here and the backend keeps it, so a stop or a rewind
// can name a turn by an id this side already has.
function newTurnId(): string {
  return `t_${Array.from(crypto.getRandomValues(new Uint8Array(5)), (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

function blank(id: string, at: number): Turn {
  return { id, at, ask: "", feed: null, notice: null, answer: "", thinking: "", parts: [], running: false, stopping: false, ending: null };
}

function set(store: Store, turns: Turn[]): void {
  const running = turns.filter((t) => t.running);
  store.state = { turns, running: running.length > 0, stopping: running.some((t) => t.stopping), wake: store.state.wake };
  if (store === active) emit();
}

function patch(store: Store, id: string, change: Partial<Turn>): void {
  const i = store.state.turns.findIndex((t) => t.id === id);
  if (i < 0) return;
  const turns = store.state.turns.slice();
  turns[i] = { ...turns[i], ...change };
  set(store, turns);
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

// Reads one turn's stream into the turn, until the backend ends it. A
// stream lost on the way (a dropped connection, a phone in the background)
// says nothing about the turn, which the backend runs on, so a turn the
// backend has is followed again from its start, once.
async function read(store: Store, id: string, replies: (signal: AbortSignal) => AsyncGenerator<Reply>, again = true): Promise<void> {
  const abort = new AbortController();
  store.streams.set(id, abort);
  try {
    for await (const reply of replies(abort.signal)) {
      store.known.add(id);
      patch(store, id, reply);
    }
    patch(store, id, { running: false, stopping: false, thinking: "" });
  } catch (err) {
    if (abort.signal.aborted) return;
    if (again && store.known.has(id) && !store.dropped.has(id)) {
      console.warn("thread: lost the reply's stream, following it again", err);
      void read(store, id, (signal) => followTurn(id, signal), false);
      return;
    }
    console.error("thread: reply failed", err);
    patch(store, id, { running: false, stopping: false, thinking: "", ending: "failed" });
  } finally {
    if (store.streams.get(id) === abort) store.streams.delete(id);
  }
}

// Follows a turn this client has no stream of: one it didn't start, or
// one running since before a restart. Unknown here, it is added first.
function follow(store: Store, id: string, at = Date.now()): void {
  if (store.streams.has(id) || store.dropped.has(id)) return;
  if (!store.state.turns.some((t) => t.id === id)) set(store, insert(store.state.turns, [{ ...blank(id, at), running: true }]));
  else patch(store, id, { running: true, ending: null });
  void read(store, id, (signal) => followTurn(id, signal));
}

// Drops the turns from `keep` on, asks `ask` in their place, and streams
// the answer into the new turn. A wake only ever asks at its end.
function answer(store: Store, keep: number, ask: string, mode: InputMode): void {
  const kept = store.state.turns.slice(0, keep);
  const gone = store.state.turns.slice(keep);
  // The backend replaces from the first dropped turn it knows of.
  const replaces = gone.find((t) => store.known.has(t.id))?.id;
  for (const t of gone) {
    store.dropped.add(t.id);
    store.streams.get(t.id)?.abort();
  }
  const turn: Turn = { ...blank(newTurnId(), Date.now()), ask, running: true };
  set(store, [...kept, turn]);
  const wake = store.state.wake ?? undefined;
  void read(store, turn.id, (signal) => streamTurn({ id: turn.id, ask, replaces, actions: actionsAfterLastAsk(kept), mode, wake }, signal));
}

function indexOf(id: string): number {
  const i = active.state.turns.findIndex((t) => t.id === id);
  if (i < 0) throw new Error(`thread: no turn ${id}`);
  return i;
}

// How an ask was made: spoken in voice mode, or typed.
export type InputMode = "voice" | "text";

export function ask(text: string, mode: InputMode): void {
  answer(active, active.state.turns.length, text, mode);
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
  set(active, [...active.state.turns, { ...blank(id, Date.now()), feed, answer: feed.label, parts: [part] }]);
}

// Rewind: the turn and everything after it are replaced by a new ask. A
// wake has no rewind: Hermes keeps everything its run did.
export function edit(id: string, text: string, mode: InputMode): void {
  if (active.state.wake) return;
  answer(active, indexOf(id), text, mode);
}

// Asks the same thing again. Turns after it are dropped.
export function retry(id: string, mode: InputMode): void {
  if (active.state.wake) return;
  const i = indexOf(id);
  answer(active, i, active.state.turns[i].ask, mode);
}

// Asks the backend to stop every reply still coming in. Each keeps
// streaming until the backend ends it, and its `turn.ended` says whether
// Hermes stopped or finished anyway.
export function stop(): void {
  const store = active;
  for (const turn of store.state.turns) {
    if (!turn.running || turn.stopping) continue;
    patch(store, turn.id, { stopping: true });
    void stopTurn(turn.id);
  }
}

// The push stream carries this device's turn events too, and those of
// its user's wakes. A turn this client streams already, or one that is
// over, needs nothing. Any other (an ask that reached this device some
// other way, or one running since before the stream dropped) is followed
// from its start. A wake's turn matters only while that wake is open.
export function heard(id: string, wake: string | null): void {
  const store = wake === null ? own : wakeStore?.state.wake === wake ? wakeStore : null;
  if (!store) return;
  const mine = store.state.turns.find((t) => t.id === id);
  if (store.streams.has(id) || store.dropped.has(id) || (mine && !mine.running)) return;
  follow(store, id);
}

export interface NoticeData {
  id: string;
  at?: string;
  text: string;
  image?: string | null;
  urgency?: string;
  opens?: string | null;
  from?: string | null;
}

// Adds notifications as turns of their own, once each, to the device's own
// conversation. Those from before `seenUntil` were shown already; later
// ones from before this launch take the stage the way a fresh turn would.
export function notices(sent: NoticeData[], seenUntil: number | null): void {
  const have = new Set(own.state.turns.map((t) => t.id));
  const now = Date.now();
  const more = sent
    .filter((n) => !have.has(n.id) && !own.dropped.has(n.id))
    .map((n): Turn => {
      const at = n.at ? Date.parse(n.at) : now;
      const urgency = n.urgency === "low" || n.urgency === "high" ? n.urgency : "normal";
      const notice: Notice = { id: n.id, urgency, opens: n.opens ?? null, from: n.from ?? null, unseen: n.at !== undefined && seenUntil !== null && at > seenUntil };
      return { ...blank(n.id, n.at ? at : now), notice, answer: n.text, parts: n.image ? [{ kind: "image", src: n.image }] : [] };
    });
  if (more.length === 0) return;
  // A reply still coming in keeps the stage. The notice goes in under it,
  // and waits in history.
  const turns = own.state.turns;
  const reply = turns.findIndex((t) => t.running && asked(t));
  set(own, reply < 0 ? insert(turns, more) : [...insert(turns.slice(0, reply), more), ...turns.slice(reply)]);
}

// One notification, as the push stream brings it: it is shown now, so its
// time is this moment.
export function notify(n: NoticeData): void {
  notices([{ ...n, at: undefined }], null);
}

// Merges what the backend keeps into a store: turns this client doesn't
// have are added, one that ended while nobody here listened takes the
// backend's ending, and one still running is followed from its start. A
// turn that failed here may have finished there: its stream was lost, not
// the turn.
function merge(store: Store, kept: KeptTurn[]): void {
  const local = new Map(store.state.turns.map((t) => [t.id, t]));
  const added: Turn[] = [];
  let turns = store.state.turns;
  for (const k of kept) {
    store.known.add(k.id);
    const mine = local.get(k.id);
    if (store.dropped.has(k.id) || store.streams.has(k.id)) continue;
    const turn: Turn = { ...blank(k.id, Date.parse(k.at)), ask: k.ask, answer: k.answer.trim(), parts: k.parts, running: k.running, ending: k.ending };
    if (!mine) added.push(turn);
    else if ((mine.running || mine.ending === "failed") && !k.running) turns = turns.map((t) => (t.id === k.id ? turn : t));
  }
  set(store, insert(turns, added));
  for (const k of kept) if (k.running) follow(store, k.id);
}

// Catches up with the backend: at launch, and each time the push stream
// connects again. The wake on the stage catches up too.
export async function sync(): Promise<void> {
  const wake = wakeStore;
  try {
    const [mine, theirs] = await Promise.all([keptTurns(), wake ? keptTurns(wake.state.wake) : Promise.resolve(null)]);
    merge(own, mine);
    if (theirs && wake === wakeStore && wake) merge(wake, theirs);
  } catch (err) {
    console.error("thread: could not load the conversation", err);
  }
}

// Puts a wake's conversation on the stage, and loads its turns.
export async function showWake(id: string): Promise<void> {
  if (wakeStore?.state.wake !== id) {
    leaveWakeStore();
    wakeStore = newStore(id);
  }
  const store = wakeStore;
  active = store;
  emit();
  try {
    merge(store, await keptTurns(id));
  } catch (err) {
    console.error("thread: could not load the wake", err);
  }
}

// Puts the device's own conversation back on the stage.
export function showOwn(): void {
  leaveWakeStore();
  active = own;
  emit();
}

function leaveWakeStore(): void {
  if (!wakeStore) return;
  for (const abort of wakeStore.streams.values()) abort.abort();
  wakeStore = null;
}

// Forgets everything: the device logged out, and the next one to log in
// may be someone else.
export function reset(): void {
  leaveWakeStore();
  for (const abort of own.streams.values()) abort.abort();
  own.streams.clear();
  own.known.clear();
  own.dropped.clear();
  active = own;
  set(own, []);
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getThread(): ThreadState {
  return active.state;
}

export function useThread(): ThreadState {
  return useSyncExternalStore(subscribe, getThread);
}
