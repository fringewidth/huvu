import { PAPER, THREAD } from "../config/palette";
import { createFlower, fillPetal, paintCore, paintPetal, petalOutline } from "./flowerGenerator";
import { nextSpecies } from "./flowers";
import { buildStrand, drawStrandGrowth, growStrand, growthCap, strandDone, type Strand } from "./garland";
import { paintLeaf } from "./leaves";
import type { Brush, Flower, PetalShape } from "./types";
import { pick, rand, randInt, TAU } from "./util";

// The garden: a garland that grows while huvu talks (or writes), and
// flowers opening along its thread. What is painted stays. The loop sleeps
// once there is nothing left to paint. It runs in a worker or on the page
// (host.ts), so it knows nothing of either: what it hears and reads comes
// in through GardenInput.
//
// It used to wash the page back toward paper a little every 0.3s, and wash
// flowers older than the newest eight away. Neither changed a pixel that
// could be seen (p5.brush's wash at that strength rounds to nothing), and
// on a phone's GPU each page wash stalled the screen for about half a
// second, so both went on 2026-10-10.

// Voice below this loudness counts as silence.
const GATE = 0.1;
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

interface Bloom {
  flower: Flower;
  cx: number;
  cy: number;
  s: number;
  idx: number;
  strand: Strand;
}

// huvu's voice in one frame. `onset` marks the start of a syllable.
export interface VoiceSample {
  active: boolean;
  loud: number;
  onset: boolean;
}

export interface GardenInput {
  // huvu's voice now, read once per frame.
  voice(dt: number, now: number): VoiceSample;
  // Words of a written reply since the last call.
  words(): number;
  // How long a frame took, whether it painted, and how many petals. For
  // the bench.
  frameDone?(ms: number, painted: boolean, petals: number): void;
}

export interface GardenSize {
  width: number;
  height: number;
  density: number;
}

export interface Garden {
  // Starts the loop again if it slept; call it whenever there is input.
  wake(): void;
  stop(): void;
}

// Paints the garden into a new p5.brush canvas, put in `parent` (or
// nowhere, in a worker), until stop().
export function paintGarden(brush: Brush, { width: W, height: H, density }: GardenSize, parent: HTMLElement | null, input: GardenInput): Garden {
  const R0 = Math.min(W, H) * 0.055;
  // Thread growth per second while huvu talks, from just above the gate to
  // full loudness, plus a burst on each syllable onset.
  const GROWTH_MIN = R0 * 0.85 * PACE;
  const GROWTH_MAX = R0 * 6.3 * PACE;
  const ONSET_BURST = R0 * 2.1 * PACE;
  // One flower slot is R0 * 1.55 of thread, so about one flower per 77
  // words. Stage caps how many words one reply may send.
  const WRITE_PER_WORD = R0 * 0.02;

  brush.createCanvas(W, H, { parent, pixelDensity: density });
  brush.noStroke();
  brush.noHatch();
  brush.clear(PAPER);
  brush.render();

  let strand: Strand | null = null;
  let fallen: FallenPetal[] = [];
  const queue: Bloom[] = [];
  let active: Bloom | null = null;
  let petalAcc = 0;
  // Petals painted this frame, for the bench.
  let petals = 0;
  // Thread still owed to written words. While it lasts, and until the
  // flowers it started have opened, writing stands in for a voice.
  let writeBudget = 0;
  let writeTail = false;
  let simT = 0;
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
      petals++;
      active.idx++;
      painted++;
    }
    if (active.idx < flower.poses.length) return painted > 0;

    paintCore(brush, flower, active.cx, active.cy, active.s);
    dropPetals(active);
    active.strand.completedSlots++;
    active = null;
    petalAcc = 0;
    return true;
  }

  function idle(voiceActive: boolean): boolean {
    return !voiceActive && !writeTail && !active && !queue.length && !fallen.length;
  }

  function frame(now: number): void {
    const began = performance.now();
    petals = 0;
    const dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    simT += dt;

    const words = input.words();
    if (words) {
      writeBudget += words * WRITE_PER_WORD;
      writeTail = true;
    }
    const voice = input.voice(dt, simT);

    brush.push();
    brush.translate(-W / 2, -H / 2);

    let dirty = false;
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
    }

    // Each one runs every frame; don't let one short-circuit the others.
    const bloomed = advanceBloom(dt, loud, onset);
    const fell = paintFallen();
    if (bloomed || fell) dirty = true;

    brush.pop();
    if (dirty) brush.render();
    input.frameDone?.(performance.now() - began, dirty, petals);

    asleep = idle(voice.active);
    if (!asleep) raf = requestAnimationFrame(frame);
  }

  let stopped = false;
  raf = requestAnimationFrame(frame);

  return {
    wake: () => {
      if (!asleep || stopped) return;
      asleep = false;
      last = performance.now();
      raf = requestAnimationFrame(frame);
    },
    stop: () => {
      stopped = true;
      cancelAnimationFrame(raf);
    },
  };
}
