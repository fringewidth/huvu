import type { ThreadMessage } from "@assistant-ui/react";

// The stage never shows a chat log. It shows turns: one ask and what huvu
// did with it. A turn is derived from the thread's messages on every
// change, so assistant-ui stays the single source of truth.
export type RichPart =
  | { kind: "image"; src: string }
  | { kind: "tool"; id: string; toolName: string; args: Record<string, unknown>; result: unknown };

// How huvu's reply ended, when it didn't simply finish.
export type Ending = "failed" | "stopped" | null;

export interface Turn {
  // Id of the user message that opened the turn. Rewinding edits it.
  id: string;
  // Id of huvu's reply, once there is one. Retrying reloads it.
  replyId: string | null;
  // When the ask was made. Turns older than this session (seeded or
  // restored) never grow the garland or get announced.
  at: number;
  ask: string;
  answer: string;
  parts: RichPart[];
  running: boolean;
  ending: Ending;
}

export function toTurns(messages: readonly ThreadMessage[], isRunning: boolean): Turn[] {
  const turns: Turn[] = [];
  for (const m of messages) {
    if (m.role === "user") {
      const ask = m.content.flatMap((p) => (p.type === "text" ? [p.text] : [])).join("\n");
      turns.push({ id: m.id, replyId: null, at: m.createdAt.getTime(), ask, answer: "", parts: [], running: false, ending: null });
      continue;
    }
    const turn = turns[turns.length - 1];
    if (m.role !== "assistant" || !turn) continue;
    turn.replyId = m.id;
    for (const p of m.content) {
      if (p.type === "text") turn.answer = [turn.answer, p.text.trim()].filter(Boolean).join("\n\n");
      if (p.type === "image") turn.parts.push({ kind: "image", src: p.image });
      if (p.type === "tool-call") {
        turn.parts.push({ kind: "tool", id: p.toolCallId, toolName: p.toolName, args: p.args, result: p.result });
      }
    }
    if (m.status.type === "incomplete") turn.ending = m.status.reason === "cancelled" ? "stopped" : "failed";
  }
  const last = turns[turns.length - 1];
  if (last) last.running = isRunning;
  return turns;
}

function samePart(a: RichPart, b: RichPart): boolean {
  if (a.kind === "image") return b.kind === "image" && a.src === b.src;
  return b.kind === "tool" && a.id === b.id && a.args === b.args && a.result === b.result;
}

function sameTurn(a: Turn, b: Turn): boolean {
  return (
    a.id === b.id &&
    a.replyId === b.replyId &&
    a.ask === b.ask &&
    a.answer === b.answer &&
    a.running === b.running &&
    a.ending === b.ending &&
    a.parts.length === b.parts.length &&
    a.parts.every((p, i) => samePart(p, b.parts[i]))
  );
}

// Keeps the previous object for every turn that hasn't changed, so a
// streaming reply re-renders only the turn it belongs to.
export function reuseTurns(next: Turn[], prev: Turn[]): Turn[] {
  const byId = new Map(prev.map((t) => [t.id, t]));
  return next.map((t) => {
    const old = byId.get(t.id);
    return old && sameTurn(old, t) ? old : t;
  });
}
