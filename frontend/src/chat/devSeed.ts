import type { RichPart, Turn } from "./thread";
import { BACKEND } from "./backend";
import { PLATE } from "../config/palette";

// A few past turns with images, a web search, a live feed and choices,
// so the stage's rich parts and the history's chips can be seen before the
// real gateway sends any. Dev builds only, and only with ?seed in the URL.
export function devSeed(): Turn[] {
  if (!import.meta.env.DEV || !new URLSearchParams(window.location.search).has("seed")) return [];
  // Dated yesterday, like real history would be, so the stage treats them
  // as old turns: they don't grow the garland or get announced.
  const at = Date.now() - 86_400_000;
  return SEED.map((t, i) => ({ id: `seed-${i}`, at, parts: [], running: false, ending: null, ...t }));
}

const tool = (id: string, toolName: string, args: Record<string, unknown>, result: unknown = {}): RichPart => ({
  kind: "tool",
  id,
  toolName,
  args,
  result,
});

const SEED: (Pick<Turn, "ask" | "answer"> & { parts?: RichPart[] })[] = [
  {
    ask: "remind me to call amma at seven",
    answer: "Done. I'll nudge you at seven, and again at quarter past if you're still at your desk.",
  },
  {
    ask: "what's flowering on the balcony right now?",
    answer: "The mogra opened last night, and the hibiscus is about to.",
    parts: [
      { kind: "image", src: plate(38, 330) },
      { kind: "image", src: plate(8, 280) },
    ],
  },
  {
    ask: "find me a dentist near indiranagar, open saturday",
    answer: "Smile Studio on 12th Main. Kindest reviews, two Saturday slots left.",
    parts: [
      tool("seed-search", "web_search", { query: "dentist indiranagar saturday" }, { sources: [1, 2, 3, 4, 5, 6] }),
      // The mock backend streams a test pattern here.
      tool("seed-feed", "video_feed", {
        src: `${BACKEND}/api/feed/demo`,
        label: "huvu's browser",
        caption: "smilestudio.example/book/saturday",
      }),
      tool("seed-choices", "choices", { options: ["book 10:30", "book 12:00", "the other two"] }),
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
    <rect width="400" height="300" fill="${PLATE}"/>
    <rect width="400" height="300" fill="url(#b)"/>
    <rect width="400" height="300" fill="url(#a)"/>
  </svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}
