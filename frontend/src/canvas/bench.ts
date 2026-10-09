import { seedRandom } from "./util";

// With ?bench in the URL, in a dev build or one made with VITE_BENCH=1:
// what the garden costs a real device, the same on every run. Math.random
// repeats from a fixed seed, and a debugger starts a made-up voice with
// window.__bench.speak(seconds) and reads window.__bench.stats. The driver
// that runs it on a phone is frontend/bench/.

export const BENCH = (import.meta.env.DEV || import.meta.env.VITE_BENCH === "1") && new URLSearchParams(window.location.search).has("bench");

export interface BenchVoice {
  active: boolean;
  loud: number;
  onset: boolean;
}

// How long the garden's frames took, and how many of them painted.
const stats = { frames: 0, painted: 0, workMs: 0, worstMs: 0, petals: 0, long: [] as [number, number][] };
let until = 0;
let started = 0;
let lastSyllable = -1;
let wake: (() => void) | null = null;

if (BENCH) {
  seedRandom();
  (window as unknown as { __bench: unknown }).__bench = {
    stats,
    speak(seconds: number) {
      Object.assign(stats, { frames: 0, painted: 0, workMs: 0, worstMs: 0, petals: 0, long: [] });
      started = performance.now();
      until = started + seconds * 1000;
      lastSyllable = -1;
      wake?.();
    },
  };
}

export function setBenchWake(fn: (() => void) | null): void {
  wake = fn;
}

// Speech at about five syllables a second, loud and uneven, like huvu
// answering at length.
export function benchVoice(now: number): BenchVoice | null {
  if (!BENCH || now > until) return null;
  const t = (now - started) / 1000;
  const syllable = Math.floor(t * 5);
  const within = t * 5 - syllable;
  const onset = syllable !== lastSyllable;
  lastSyllable = syllable;
  return { active: true, loud: 0.4 + 0.5 * Math.abs(Math.sin(syllable * 1.7)) * (1 - within * 0.6), onset };
}

export function benchFrame(ms: number, painted: boolean, petals: number): void {
  benchFrames(1, painted ? 1 : 0, ms, ms, petals, ms > 50 ? [[performance.timeOrigin + performance.now() - ms, ms]] : []);
}

// Frames counted elsewhere (in the worker) and sent over in batches.
// `long` lists frames over 50ms: when each began (timeOrigin + now) and how
// long it took.
export function benchFrames(frames: number, painted: number, workMs: number, worstMs: number, petals: number, long: [number, number][] = []): void {
  if (!BENCH) return;
  stats.long.push(...long);
  stats.petals += petals;
  stats.frames += frames;
  stats.painted += painted;
  stats.workMs += workMs;
  stats.worstMs = Math.max(stats.worstMs, worstMs);
}
