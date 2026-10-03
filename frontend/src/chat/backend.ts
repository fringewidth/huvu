import type { Ending, Reply, RichPart } from "./thread";

// !todo our frontend is gonna need auth lol
export const BACKEND = "http://localhost:3001";
const CHAT_ENDPOINT = `${BACKEND}/api/chat`;

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
}

// What an event's data can carry. Which fields are there depends on the
// event; the contract is in docs/frontend-contract.md.
interface EventData {
  text?: string;
  part?: RichPart;
  id?: string;
  result?: unknown;
  ending?: Ending;
  answer?: string;
}

// Streams one turn. Each value is the turn so far: the whole answer and
// every part. The first value is empty and says the backend has the turn.
export async function* streamTurn(body: AskBody, signal: AbortSignal): AsyncGenerator<Reply> {
  const res = await fetch(CHAT_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`chat: backend answered ${res.status}`);
  let answer = "";
  let parts: RichPart[] = [];
  let ended = false;
  for await (const { name, data } of events(res.body)) {
    if (name === "turn.started") {
      yield {};
    } else if (name === "answer.delta") {
      answer += data.text ?? "";
      yield { answer: answer.trim() };
    } else if (name === "part" && data.part) {
      parts = [...parts, data.part];
      yield { parts };
    } else if (name === "part.update") {
      parts = parts.map((p) => (p.kind === "tool" && p.id === data.id ? { ...p, result: data.result } : p));
      yield { parts };
    } else if (name === "turn.ended") {
      ended = true;
      yield { answer: (data.answer ?? answer).trim(), ending: data.ending ?? null };
    }
  }
  if (!ended) throw new Error("chat: the stream ended before the turn did");
}

// Dropping the stream doesn't stop a turn, since other screens may be
// showing it. This does.
export async function stopTurn(id: string): Promise<void> {
  try {
    await fetch(`${BACKEND}/api/turns/${id}/stop`, { method: "POST" });
  } catch (err) {
    console.error("chat: could not stop the turn", err);
  }
}

// Server-Sent Events: blocks of `event:` and `data:` lines, ended by a
// blank line. A block with only a comment (a keepalive) says nothing.
async function* events(body: ReadableStream<Uint8Array>): AsyncGenerator<{ name: string; data: EventData }> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    for (let end = buffer.indexOf("\n\n"); end >= 0; end = buffer.indexOf("\n\n")) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let name = "message";
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length > 0) yield { name, data: JSON.parse(data.join("\n")) as EventData };
    }
  }
}
