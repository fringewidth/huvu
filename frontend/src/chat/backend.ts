import type { Reply } from "./thread";

// !todo our frontend is gonna need auth lol
export const BACKEND = "http://localhost:3001";
const CHAT_ENDPOINT = `${BACKEND}/api/chat`;

export interface HistoryMessage {
  role: "user" | "assistant";
  text: string;
}

// Streams huvu's reply to the last message in `history`. Each value is the
// whole reply so far. The mock backend sends plain text; the real gateway
// will also send tool calls and images as `parts`.
export async function* streamReply(history: HistoryMessage[], signal: AbortSignal): AsyncGenerator<Reply> {
  const res = await fetch(CHAT_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages: history }),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`chat: backend answered ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let text = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    text += decoder.decode(value, { stream: true });
    yield { answer: text.trim() };
  }
}
