import type { ThreadMessageLike } from "@assistant-ui/react";

// A few past turns with images, a web search, a live browser and choices,
// so the stage's rich parts and the history's chips can be seen before the
// real gateway sends any. Dev builds only, and only with ?seed in the URL.
export function devSeed(): ThreadMessageLike[] | undefined {
  if (!import.meta.env.DEV || !new URLSearchParams(window.location.search).has("seed")) return undefined;
  // Dated yesterday, like real history would be, so the stage treats them
  // as old turns: they don't grow the garland or get announced.
  const yesterday = new Date(Date.now() - 86_400_000);
  return SEED.map((m) => ({ ...m, createdAt: yesterday }));
}

const SEED: ThreadMessageLike[] = [
  { role: "user", content: "remind me to call amma at seven" },
  { role: "assistant", content: "Done. I'll nudge you at seven, and again at quarter past if you're still at your desk." },
  { role: "user", content: "what's flowering on the balcony right now?" },
  {
    role: "assistant",
    content: [
      { type: "text", text: "The mogra opened last night, and the hibiscus is about to." },
      { type: "image", image: plate(38, 330) },
      { type: "image", image: plate(8, 280) },
    ],
  },
  { role: "user", content: "find me a dentist near indiranagar, open saturday" },
  {
    role: "assistant",
    content: [
      {
        type: "tool-call",
        toolCallId: "seed-search",
        toolName: "web_search",
        args: { query: "dentist indiranagar saturday" },
        result: { sources: [1, 2, 3, 4, 5, 6] },
      },
      { type: "tool-call", toolCallId: "seed-browser", toolName: "browser", args: { url: "smilestudio.example/book/saturday" }, result: {} },
      { type: "text", text: "Smile Studio on 12th Main. Kindest reviews, two Saturday slots left." },
      {
        type: "tool-call",
        toolCallId: "seed-choices",
        toolName: "choices",
        args: { options: ["book 10:30", "book 12:00", "the other two"] },
        result: {},
      },
    ],
  },
];

// A soft two-hue wash standing in for a photo.
function plate(hueA: number, hueB: number): string {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300">
    <defs>
      <radialGradient id="a" cx="30%" cy="40%" r="60%"><stop offset="0" stop-color="hsl(${hueA} 70% 62%)"/><stop offset="1" stop-color="hsl(${hueA} 70% 62%)" stop-opacity="0"/></radialGradient>
      <radialGradient id="b" cx="70%" cy="65%" r="55%"><stop offset="0" stop-color="hsl(${hueB} 45% 48%)"/><stop offset="1" stop-color="hsl(${hueB} 45% 48%)" stop-opacity="0"/></radialGradient>
    </defs>
    <rect width="400" height="300" fill="#141522"/>
    <rect width="400" height="300" fill="url(#b)"/>
    <rect width="400" height="300" fill="url(#a)"/>
  </svg>`;
  // Base64, not URL-encoded: assistant-ui drops an assistant message's
  // image unless it is https, blob, or a data URL it can parse this way.
  return `data:image/svg+xml;base64,${btoa(svg)}`;
}
