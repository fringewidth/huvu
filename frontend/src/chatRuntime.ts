import type { ChatModelAdapter } from "@assistant-ui/react";

// !todo our frontend is gonna need auth lol
const CHAT_ENDPOINT = "http://localhost:3001/api/chat";

export const mockChatAdapter: ChatModelAdapter = {
  async *run({ messages, abortSignal }) {
    const response = await fetch(CHAT_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages }),
      signal: abortSignal,
    });

    const reader = response.body!.getReader();
    const decoder = new TextDecoder();
    let text = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      text += decoder.decode(value, { stream: true });
      yield { content: [{ type: "text", text }] };
    }
  },
};
