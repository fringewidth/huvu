import { call, getJson, sse } from "../net/http";
import type { Ending, Reply, RichPart } from "./thread";

// Something the owner did in the app since their last ask, such as opening
// a live feed. It rides with the next ask as context for the model. It is
// never a request, so it gets no reply of its own.
export interface Action {
  text: string;
  // ISO time.
  at: string;
}

export interface AskBody {
  // The turn's own id. The backend keeps it, so a stop or a rewind can
  // name the turn by an id the client already has.
  id: string;
  ask: string;
  // The first turn this ask replaces, with everything after it (edit and
  // retry). Left out for a new turn at the end.
  replaces?: string;
  actions: Action[];
  // How the ask was made: spoken in voice mode, or typed. huvu writes a
  // spoken answer to be heard.
  mode: "voice" | "text";
  // The wake it is asked in, not the device's own conversation. A wake
  // takes no `replaces`.
  wake?: string;
}

// What a turn event's data can carry. Which fields are there depends on
// the event; the contract is in docs/frontend-contract.md.
export interface TurnEventData {
  turn: string;
  // Set on a wake's turn: the wake it belongs to.
  wake?: string;
  ask?: string;
  at?: string;
  text?: string;
  // On thinking.delta: the first piece of a new burst of thinking.
  start?: boolean;
  part?: RichPart;
  id?: string;
  result?: unknown;
  ending?: Ending;
  answer?: string;
}

export const TURN_EVENTS = new Set(["turn.started", "answer.delta", "thinking.delta", "part", "part.update", "turn.ended"]);

// A turn as the backend keeps it (`GET /api/conversation`).
export interface KeptTurn {
  id: string;
  at: string;
  ask: string;
  answer: string;
  parts: RichPart[];
  ending: Ending;
  running: boolean;
}

// Streams one turn. Each value is the turn so far: the whole answer and
// every part. The first value is empty and says the backend has the turn.
export async function* streamTurn(body: AskBody, signal: AbortSignal): AsyncGenerator<Reply> {
  const res = await call("/api/chat", { method: "POST", json: body, signal });
  yield* readTurn(res);
}

// Follows a turn this client didn't start, or lost the stream of, from
// its start: the backend replays what came before, then goes on live. A
// delta doesn't say where its text goes, so a turn joined halfway can only
// be rebuilt this way, never by adding later deltas to a snapshot.
export async function* followTurn(id: string, signal: AbortSignal): AsyncGenerator<Reply> {
  const res = await call(`/api/turns/${id}/events`, { signal });
  yield* readTurn(res);
}

async function* readTurn(res: Response): AsyncGenerator<Reply> {
  if (!res.body) throw new Error("chat: the backend sent no stream");
  let answer = "";
  let thinking = "";
  let parts: RichPart[] = [];
  let ended = false;
  for await (const { name, data } of sse<TurnEventData>(res.body)) {
    if (name === "turn.started") {
      // A followed turn learns its ask here.
      yield data.ask === undefined ? {} : { ask: data.ask };
    } else if (name === "answer.delta") {
      answer += data.text ?? "";
      yield { answer: answer.trim() };
    } else if (name === "thinking.delta") {
      thinking = data.start ? (data.text ?? "") : thinking + (data.text ?? "");
      yield { thinking };
    } else if (name === "part" && data.part) {
      const part = data.part;
      parts = [...parts.filter((p) => p.kind !== "tool" || part.kind !== "tool" || p.id !== part.id), part];
      yield { parts };
    } else if (name === "part.update") {
      parts = parts.map((p) => (p.kind === "tool" && p.id === data.id ? { ...p, result: data.result } : p));
      yield { parts };
    } else if (name === "turn.ended") {
      ended = true;
      // The whole answer, which is the truth however the deltas went.
      yield { answer: (data.answer ?? answer).trim(), thinking: "", ending: data.ending ?? null };
    }
  }
  if (!ended) throw new Error("chat: the stream ended before the turn did");
}

// Asks the backend to stop a turn. Hermes may still finish it: the turn's
// stream goes on until `turn.ended`, which says how it really ended.
export async function stopTurn(id: string): Promise<void> {
  try {
    await call(`/api/turns/${id}/stop`, { method: "POST" });
  } catch (err) {
    console.error("chat: could not stop the turn", err);
  }
}

// The device's own turns, or a wake's.
export async function keptTurns(wake: string | null = null): Promise<KeptTurn[]> {
  const kept = wake === null ? await getJson<KeptTurn[]>("/api/conversation") : (await getJson<{ turns: KeptTurn[] }>(`/api/wakes/${wake}`)).turns;
  return kept;
}
