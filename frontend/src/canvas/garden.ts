import { ui } from "../config/interaction";
import { PAPER, THREAD } from "../config/palette";
import { onSpeakingStart, sampleVoice } from "../voice/speaker";
import { createFlower, fillPetal, paintCore, paintPetal, petalOutline } from "./flowerGenerator";
import { nextSpecies } from "./flowers";
import { buildStrand, drawStrandGrowth, growStrand, growthCap, strandDone, type Strand } from "./garland";
import { paintLeaf } from "./leaves";
import type { Brush, Flower, PetalShape, Point } from "./types";
import { pick, rand, randInt, TAU } from "./util";

// The garden: a garland that grows while huvu talks (or writes), flowers
// opening along its thread, and the whole page slowly washing back toward
// paper. The loop sleeps once there is nothing left to paint or fade.

// Starts loading at boot, alongside React's first render, on a screen
// that has a garden at all.
const brushModule = ui.garden ? (import("p5.brush/standalone") as unknown as Promise<Brush>) : null;

// A wash over the whole page every FADE_EVERY seconds, until SETTLE_S
// after the last stroke, by when the washes no longer change anything.
const FADE_EVERY = 0.3;
const FADE_ALPHA = 4;
const SETTLE_S = 110;
// Voice below this loudness counts as silence.
const GATE = 0.1;
// Flowers older than the newest TAIL_FLOWERS fade away over TAIL_FADE_FRAMES.
const TAIL_FLOWERS = 8;
const TAIL_FADE_ALPHA = 4;
const TAIL_FADE_FRAMES = 130;
// How fast the garden grows, as a share of its first tuning: the thread,
// the bursts on syllables and the petals all at once, so spoken and
// written replies slow alike. A third since 2026-10-09; it grew faster
// than anyone could watch.
const PACE = 1 / 3;
const PETAL_RATE_MIN = 5 * PACE;
const PETAL_RATE_MAX = 26 * PACE;
const ONSET_PETALS = 2 * PACE;
// Written replies grow the garland as if huvu said them at a steady,
// moderate volume.
const WRITE_LOUD = 0.45;

interface FallenPetal {
  t: number;
  x: number;
  y: number;
  theta: number;
  len: number;
  aspect: number;
  alpha: number;
  color: string;
  shape: PetalShape;
}

interface TrailEntry {
  cx: number;
  cy: number;
  rx: number;
  ry: number;
  slot: number;
  fade: number;
}

interface Bloom {
  flower: Flower;
  cx: number;
  cy: number;
  s: number;
  idx: number;
  strand: Strand;
}

let wordsOwed = 0;
let wake: (() => void) | null = null;

// A written reply grows the garland a little per new word.
export function growForWords(words: number): void {
  wordsOwed += words;
  wake?.();
}

// Paints the garden into `container` until the returned function is called.
export function startGarden(container: HTMLElement): () => void {
  let stop: (() => void) | null = null;
  let cancelled = false;
  void brushModule?.then((brush) => {
    if (!cancelled) stop = run(brush, container);
  });
  return () => {
    cancelled = true;
    stop?.();
  };
}

function run(brush: Brush, container: HTMLElement): () => void {
  const W = window.innerWidth;
  const H = window.innerHeight;
  const R0 = Math.min(W, H) * 0.055;
  // Thread growth per second while huvu talks, from just above the gate to
  // full loudness, plus a burst on each syllable onset.
  const GROWTH_MIN = R0 * 0.85 * PACE;
  const GROWTH_MAX = R0 * 6.3 * PACE;
  const ONSET_BURST = R0 * 2.1 * PACE;
  // One flower slot is R0 * 1.55 of thread, so about one flower per 77
  // words. Stage caps how many words one reply may send.
  const WRITE_PER_WORD = R0 * 0.02;

  brush.createCanvas(W, H, { parent: container, pixelDensity: Math.min(window.devicePixelRatio || 1, ui.maxPixelDensity) });
  brush.noStroke();
  brush.noHatch();
  brush.clear(PAPER);
  brush.render();

  let strand: Strand | null = null;
  let fallen: FallenPetal[] = [];
  let trail: TrailEntry[] = [];
  let slot = 0;
  const queue: Bloom[] = [];
  let active: Bloom | null = null;
  let petalAcc = 0;
  // Thread still owed to written words. While it lasts, and until the
  // flowers it started have opened, writing stands in for a voice.
  let writeBudget = 0;
  let writeTail = false;
  let simT = 0;
  let fadeAcc = 0;
  let lastInk = 0;
  let last = performance.now();
  let raf = 0;
  let asleep = false;

  function queueFlower(on: Strand, i: number): void {
    const spec = nextSpecies();
    const flower = createFlower(spec);
    const [cx, cy] = on.slots[i];
    queue.push({ flower, cx, cy, s: R0 * (spec.size ?? 1) * rand(0.7, 1.05), idx: 0, strand: on });
    if (i > 0 && (i + on.leafPhase) % 3 === 0 && spec.leaf !== "none") {
      const prev = on.slots[i - 1];
      paintLeaf(brush, (prev[0] + cx) / 2, (prev[1] + cy) / 2 + R0 * 0.2, on.green, spec.leaf, R0);
    }
  }

  // A couple of petals drift down under a flower once it has opened.
  function dropPetals({ flower, cx, cy, s }: Bloom): void {
    for (let k = 0, n = randInt(2, 3); k < n; k++) {
      const p = pick(flower.poses);
      const a = rand(0, TAU);
      fallen.push({
        t: simT + 0.3 + k * 0.4,
        x: cx + Math.cos(a) * s * 1.2 + rand(-0.08, 0.08) * W,
        y: cy + s * (1.4 + k * 1.3),
        theta: a + Math.PI / 2 + rand(-0.6, 0.6),
        len: p.len * s * rand(0.7, 1),
        aspect: p.aspect,
        alpha: 70 - k * 10,
        color: flower.pal(p.tone),
        shape: p.shape!,
      });
    }
  }

  function paintFallen(): boolean {
    const due = fallen.filter((p) => p.t <= simT);
    if (!due.length) return false;
    fallen = fallen.filter((p) => p.t > simT);
    for (const p of due) {
      fillPetal(brush, petalOutline(p.x, p.y, p.theta, 0, p.len, p.len * p.aspect, 0.15, p.shape), p.color, p.alpha, 0.4, 0.4);
    }
    return true;
  }

  const fading = (e: TrailEntry) => slot - e.slot > TAIL_FLOWERS;

  // Washes old flowers back into the paper, three per frame at most.
  function fadeTail(): boolean {
    let painted = 0;
    for (const e of trail) {
      if (!fading(e)) continue;
      if (painted >= 3) break;
      brush.noFill();
      for (const sc of [1.0, 0.8, 0.6]) {
        const pts: Point[] = [];
        for (let k = 0; k < 16; k++) {
          const a = (k / 16) * TAU;
          const rr = sc * (1 + 0.12 * Math.sin(a * 3 + e.fade * 0.3 + sc * 5));
          pts.push([e.cx + Math.cos(a) * e.rx * rr, e.cy + Math.sin(a) * e.ry * rr]);
        }
        brush.wash(PAPER, TAIL_FADE_ALPHA);
        brush.polygon(pts);
      }
      brush.noWash();
      e.fade++;
      painted++;
    }
    trail = trail.filter((e) => e.fade < TAIL_FADE_FRAMES);
    return painted > 0;
  }

  // Paints the open flower's next petals, faster the louder huvu is.
  function advanceBloom(dt: number, loud: number, onset: boolean): boolean {
    active ??= queue.shift() ?? null;
    if (!active) return false;
    if (loud > GATE) {
      petalAcc += (PETAL_RATE_MIN + (PETAL_RATE_MAX - PETAL_RATE_MIN) * Math.pow(loud, 1.3)) * dt + (onset ? ONSET_PETALS : 0);
    } else {
      petalAcc = Math.min(petalAcc, 0.5);
    }

    const { flower } = active;
    let painted = 0;
    while (petalAcc >= 1 && painted < 4 && active.idx < flower.poses.length) {
      petalAcc -= 1;
      paintPetal(brush, flower, flower.poses[active.idx], active.cx, active.cy, active.s, { loud, accent: onset && painted === 0 });
      active.idx++;
      painted++;
    }
    if (active.idx < flower.poses.length) return painted > 0;

    paintCore(brush, flower, active.cx, active.cy, active.s);
    slot++;
    trail.push({ cx: active.cx, cy: active.cy + active.s * 0.5, rx: active.s * 1.6, ry: active.s * 2.1, slot, fade: 0 });
    dropPetals(active);
    active.strand.completedSlots++;
    active = null;
    petalAcc = 0;
    return true;
  }

  function idle(voiceActive: boolean): boolean {
    return (
      simT - lastInk >= SETTLE_S &&
      !voiceActive &&
      !writeTail &&
      !active &&
      !queue.length &&
      !fallen.length &&
      !trail.some(fading)
    );
  }

  function frame(now: number): void {
    const dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    simT += dt;
    fadeAcc += dt;

    if (wordsOwed) {
      writeBudget += wordsOwed * WRITE_PER_WORD;
      writeTail = true;
      wordsOwed = 0;
    }
    const voice = sampleVoice(dt, simT);

    brush.push();
    brush.translate(-W / 2, -H / 2);

    let dirty = false;
    if (fadeAcc >= FADE_EVERY && simT - lastInk < SETTLE_S) {
      fadeAcc = 0;
      dirty = true;
      brush.noFill();
      brush.wash(PAPER, FADE_ALPHA);
      brush.rect(0, 0, W, H, "corner");
      brush.noWash();
    }

    if (writeTail && !voice.active && writeBudget <= 0 && !active && !queue.length) writeTail = false;
    const writing = writeTail && !voice.active;
    const loud = writing ? WRITE_LOUD : voice.loud;
    const onset = voice.onset && !writing;
    if ((voice.active || writing) && loud > GATE) {
      if (!strand || strandDone(strand)) strand = buildStrand(W, H, R0);
      let delta = (GROWTH_MIN + (GROWTH_MAX - GROWTH_MIN) * Math.pow(loud, 1.3)) * dt + (onset ? ONSET_BURST : 0);
      if (writing) delta = Math.min(delta, writeBudget);
      delta = Math.max(0, Math.min(delta, growthCap(strand) - strand.grownDist));
      if (writing) writeBudget -= delta;
      const crossed = growStrand(strand, delta);
      drawStrandGrowth(brush, strand, THREAD, Math.max(1.5, R0 * 0.045));
      for (const i of crossed) queueFlower(strand, i);
      dirty = true;
      lastInk = simT;
    }

    // Each one runs every frame; don't let one short-circuit the others.
    const bloomed = advanceBloom(dt, loud, onset);
    const tailed = fadeTail();
    const fell = paintFallen();
    if (bloomed || tailed || fell) {
      dirty = true;
      lastInk = simT;
    }

    brush.pop();
    if (dirty) brush.render();

    asleep = idle(voice.active);
    if (!asleep) raf = requestAnimationFrame(frame);
  }

  wake = () => {
    if (!asleep) return;
    asleep = false;
    last = performance.now();
    raf = requestAnimationFrame(frame);
  };
  const stopListening = onSpeakingStart(() => wake?.());
  raf = requestAnimationFrame(frame);

  return () => {
    cancelAnimationFrame(raf);
    stopListening();
    wake = null;
    container.querySelector("canvas")?.remove();
  };
}
