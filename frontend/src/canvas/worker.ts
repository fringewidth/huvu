import * as brushModule from "p5.brush/standalone";
import { paintGarden, type Garden, type GardenInput, type VoiceSample } from "./garden";
import type { GardenMessage, WorkerMessage } from "./host";
import type { Brush } from "./types";
import { seedRandom } from "./util";

// The garden's own thread (see host.ts). The page hands over its canvas,
// the words of written replies and huvu's voice, frame by frame.

const brush = brushModule as unknown as Brush;
const report = (message: WorkerMessage) => self.postMessage(message);

// p5.brush draws every fill into a 2D canvas first and uploads it to
// WebGL. Left to the browser, those canvases live on the GPU, where each
// fill is drawn in the GPU process, which also draws the page: on a
// phone, typing lagged behind the garden. In memory, the fills are drawn
// here, on this thread, and only uploaded.
const getContext = OffscreenCanvas.prototype.getContext as (this: OffscreenCanvas, type: string, options?: object) => unknown;
(OffscreenCanvas.prototype as unknown as { getContext: typeof getContext }).getContext = function (type, options) {
  return getContext.call(this, type, type === "2d" ? { ...options, willReadFrequently: true } : options);
};

// p5.brush makes its canvas with document.createElement, and a worker has
// no document. The first canvas it asks for after a start is the one the
// page handed over; any other (it parses colours on one) is a new one.
let handed: OffscreenCanvas | null = null;
(globalThis as unknown as { document: unknown }).document = {
  createElement: () => {
    const canvas = handed ?? new OffscreenCanvas(1, 1);
    handed = null;
    return Object.assign(canvas, { style: {} });
  },
};

let garden: Garden | null = null;
let words = 0;
let voice: VoiceSample = { active: false, loud: 0, onset: false };
// The bench's frame counts, sent to the page in batches.
const batch = { frames: 0, painted: 0, workMs: 0, worstMs: 0, petals: 0, long: [] as [number, number][] };
let counting = false;

const input: GardenInput = {
  // A syllable's onset counts in one frame only, however many frames the
  // garden paints before the next sample.
  voice: () => {
    const sample = voice;
    voice = { ...sample, onset: false };
    return sample;
  },
  words: () => {
    const owed = words;
    words = 0;
    return owed;
  },
  frameDone: (ms, painted, petals) => {
    if (!counting) return;
    batch.frames++;
    if (painted) batch.painted++;
    batch.petals += petals;
    batch.workMs += ms;
    batch.worstMs = Math.max(batch.worstMs, ms);
    if (ms > 50) batch.long.push([performance.timeOrigin + performance.now() - ms, ms]);
  },
};

self.addEventListener("message", (e: MessageEvent<GardenMessage>) => {
  const message = e.data;
  switch (message.kind) {
    case "start":
      garden?.stop();
      // The bench wants the same garden on every run, and its frame counts.
      if (message.seeded && !counting) {
        seedRandom();
        counting = true;
        setInterval(() => {
          if (!batch.frames) return;
          report({ kind: "frames", ...batch });
          Object.assign(batch, { frames: 0, painted: 0, workMs: 0, worstMs: 0, petals: 0, long: [] });
        }, 250);
      }
      handed = message.canvas;
      try {
        garden = paintGarden(brush, message, null, input);
        report({ kind: "started" });
      } catch (err) {
        report({ kind: "failed", error: String(err) });
      }
      break;
    case "stop":
      garden?.stop();
      garden = null;
      break;
    case "words":
      words += message.words;
      garden?.wake();
      break;
    case "voice":
      voice = { ...message.sample, onset: message.sample.onset || voice.onset };
      garden?.wake();
      break;
  }
});
