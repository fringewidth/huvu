import { ui } from "../config/interaction";
import { onSpeakingStart, sampleVoice } from "../voice/speaker";
import { BENCH, benchFrame, benchFrames, benchVoice, setBenchWake } from "./bench";
import type { Garden, GardenInput, GardenSize, VoiceSample } from "./garden";
import type { Brush } from "./types";

// Where the garden paints. Where the browser can hand a canvas to a worker
// (OffscreenCanvas), it paints there, so its watercolour never holds up a
// tap, a scroll or a keystroke: a frame is over 20ms of p5.brush on a
// mid-range phone (a OnePlus Nord CE 2), and on the page that halved the
// page's frame rate. Elsewhere, or if the worker fails to start, it paints
// on the page. p5.brush starts loading at boot either way, alongside
// React's first render.

export type GardenMessage =
  | ({ kind: "start"; canvas: OffscreenCanvas; seeded: boolean } & GardenSize)
  | { kind: "stop" }
  | { kind: "words"; words: number }
  | { kind: "voice"; sample: VoiceSample };

export type WorkerMessage =
  | { kind: "started" }
  | { kind: "failed"; error: string }
  | { kind: "frames"; frames: number; painted: number; workMs: number; worstMs: number; petals: number; long: [number, number][] };

const OFFSCREEN = typeof Worker !== "undefined" && "transferControlToOffscreen" in HTMLCanvasElement.prototype;
let worker = OFFSCREEN ? new Worker(new URL("./worker.ts", import.meta.url), { type: "module" }) : null;
let pageModules: Promise<[Brush, typeof import("./garden")]> | null = null;
const loadPage = () => (pageModules ??= Promise.all([import("p5.brush/standalone") as unknown as Promise<Brush>, import("./garden")]));
if (!worker) void loadPage();

// huvu's voice now: the bench's made-up one while it runs, else the
// speaker's.
const voiceNow = (dt: number, now: number): VoiceSample => benchVoice(performance.now()) ?? sampleVoice(dt, now);

let pageGarden: Garden | null = null;
let pageWords = 0;
const pageInput: GardenInput = {
  voice: voiceNow,
  words: () => {
    const words = pageWords;
    pageWords = 0;
    return words;
  },
  frameDone: benchFrame,
};

const send = (message: GardenMessage, transfer: Transferable[] = []) => worker?.postMessage(message, transfer);

// A written reply grows the garland a little per new word.
export function growForWords(words: number): void {
  if (worker) return send({ kind: "words", words });
  pageWords += words;
  pageGarden?.wake();
}

// huvu's voice plays on the page, so it is read here each frame and sent to
// the worker until it has gone quiet.
let voiceFrame = 0;
function followVoice(): void {
  if (voiceFrame || !worker) return;
  let last = performance.now();
  const tick = (now: number) => {
    const sample = voiceNow(Math.min((now - last) / 1000, 0.05), now / 1000);
    last = now;
    send({ kind: "voice", sample });
    voiceFrame = sample.active || sample.loud > 0.01 ? requestAnimationFrame(tick) : 0;
  };
  voiceFrame = requestAnimationFrame(tick);
}

// Paints the garden into `container` until the returned function is called.
export function startGarden(container: HTMLElement): () => void {
  const size: GardenSize = {
    width: window.innerWidth,
    height: window.innerHeight,
    density: Math.min(window.devicePixelRatio || 1, ui.maxPixelDensity),
  };
  let stopPage: (() => void) | null = null;
  let stopped = false;

  const paintOnPage = () => {
    void loadPage().then(([brush, { paintGarden }]) => {
      if (stopped) return;
      pageGarden = paintGarden(brush, size, container, pageInput);
      const stopListening = onSpeakingStart(() => pageGarden?.wake());
      setBenchWake(() => pageGarden?.wake());
      stopPage = () => {
        stopListening();
        pageGarden?.stop();
        pageGarden = null;
      };
    });
  };

  if (!worker) {
    paintOnPage();
    return () => {
      stopped = true;
      stopPage?.();
      setBenchWake(null);
      container.querySelector("canvas")?.remove();
    };
  }

  const canvas = document.createElement("canvas");
  canvas.style.width = `${size.width}px`;
  canvas.style.height = `${size.height}px`;
  container.appendChild(canvas);
  // A worker that can't paint (no WebGL2 there, say) hands the garden back
  // to the page, on a canvas of its own.
  const giveUp = (why: unknown) => {
    if (stopped || !worker) return;
    console.error("garden: the worker can't paint, so the page does", why);
    worker.terminate();
    worker = null;
    canvas.remove();
    paintOnPage();
  };
  const onMessage = (e: MessageEvent<WorkerMessage>) => {
    const m = e.data;
    if (m.kind === "failed") giveUp(m.error);
    else if (m.kind === "frames") benchFrames(m.frames, m.painted, m.workMs, m.worstMs, m.petals, m.long);
  };
  const onError = (e: ErrorEvent) => giveUp(e.message);
  worker.addEventListener("message", onMessage);
  worker.addEventListener("error", onError);
  // A canvas goes to a worker only once, so it goes on the next task:
  // React's development double mount stops it before then.
  const handOver = window.setTimeout(() => {
    const offscreen = canvas.transferControlToOffscreen();
    send({ kind: "start", canvas: offscreen, seeded: BENCH, ...size }, [offscreen]);
  });
  const stopListening = onSpeakingStart(followVoice);
  setBenchWake(followVoice);
  return () => {
    stopped = true;
    window.clearTimeout(handOver);
    stopListening();
    setBenchWake(null);
    worker?.removeEventListener("message", onMessage);
    worker?.removeEventListener("error", onError);
    send({ kind: "stop" });
    stopPage?.();
    canvas.remove();
    container.querySelector("canvas")?.remove();
  };
}
