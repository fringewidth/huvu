import { useEffect, useRef } from "react";
import { HUVU_SPEAK_EVENT, HUVU_SPOKE_EVENT, HUVU_WROTE_EVENT } from "../canvas/events";
import type { Brush, Point } from "../canvas/types";
import { TARGET } from "../config/interaction";
import {
  createFlower,
  fillPetal,
  hsl,
  paintCore,
  paintPetal,
  petalOutline,
  pick,
  rand,
  randInt,
  TAU,
} from "../canvas/flowerGenerator";
import { buildStrand, drawStrandGrowth, growStrand, growthCap, strandDone, type Strand } from "../canvas/garland";
import { paintLeaf } from "../canvas/leaves";
import { createAudioEngine, listenForBargeIn } from "../canvas/audio";
import type { Flower, FlowerSpec } from "../canvas/types";
import genda from "../canvas/flowers/genda";
import kamal from "../canvas/flowers/kamal";
import mogra from "../canvas/flowers/mogra";
import gudhal from "../canvas/flowers/gudhal";
import champa from "../canvas/flowers/champa";
import palash from "../canvas/flowers/palash";
import rugmini from "../canvas/flowers/rugmini";
import kaner from "../canvas/flowers/kaner";
import gulab from "../canvas/flowers/gulab";

// Placeholder species roster, ported from the recovered prototype so the
// garland has something to bloom. Not a creative decision — the owner
// redirects these once the mechanics here are solid.
const SPECIES: Record<string, () => FlowerSpec> = { genda, kamal, mogra, gudhal, champa, palash, rugmini, kaner, gulab };
const ARCH_WEIGHTS: Record<string, number> = { genda: 5, mogra: 3, gulab: 3, gudhal: 2, kamal: 1.2, champa: 1.5, kaner: 1.5, rugmini: 1.5, palash: 0.8 };

const PAPER = "#0d0e17";
const FADE_EVERY = 0.3;
const FADE_ALPHA = 4;
const GATE = 0.1;
const TAIL_FLOWERS = 8;
const TAIL_FADE_ALPHA = 4;
const TAIL_FADE_FRAMES = 130;
const THREAD = hsl(2, 0.55, 0.36);
const PETAL_RATE_MIN = 5;
const PETAL_RATE_MAX = 26;

// The mic-vs-test-tone question doesn't have a real answer yet — there's
// no Hermes/TTS audio to listen to. This clip (generated with macOS `say`)
// stands in for "the agent talking" until that pipeline exists.
const TEST_VOICE_URL = "/audio/test-voice.m4a";

interface FallenPetal {
  t: number;
  x: number;
  y: number;
  theta: number;
  len: number;
  aspect: number;
  alpha: number;
  color: string;
  shape: NonNullable<Flower["poses"][number]["shape"]>;
}

interface TrailEntry {
  cx: number;
  cy: number;
  rx: number;
  ry: number;
  slot: number;
  fade: number;
}

interface BloomJob {
  flower: Flower;
  cx: number;
  cy: number;
  s: number;
  idx: number;
  born: number;
  strandRef: Strand;
}

export function GardenCanvas() {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    let cancelled = false;
    let rafId = 0;
    let cleanup: (() => void) | undefined;

    (async () => {
      const brushModule = (await import("p5.brush/standalone")) as unknown as Brush;
      const brush = brushModule;
      if (cancelled) return;

      const A = createAudioEngine();
      const W = window.innerWidth;
      const H = window.innerHeight;
      // Phone screens are dense enough that 1.5x looks the same as 2x, and
      // it is almost half the pixels to wash and composite every frame.
      const dpr = Math.min(window.devicePixelRatio || 1, TARGET === "phone" ? 1.5 : 2);
      const R0 = Math.min(W, H) * 0.055;

      // Strand growth per second while huvu talks, from just above the
      // silence gate to full loudness, plus a burst on each syllable onset.
      const GROWTH_MIN = R0 * 0.85;
      const GROWTH_MAX = R0 * 6.3;
      const ONSET_BURST = R0 * 2.1;
      // Written replies grow the garland too: a little strand per word,
      // drawn as if huvu had said them at a steady, moderate volume. One
      // flower slot is R0 * 1.55 of strand, so this is about one flower per
      // 17 words. Stage caps how many words one reply may send.
      const WRITE_PER_WORD = R0 * 0.02;
      const WRITE_LOUD = 0.45;

      brush.createCanvas(W, H, { parent: container, pixelDensity: dpr });
      brush.noStroke();
      brush.noHatch();
      brush.clear(PAPER);
      brush.render();

      let strand: Strand | null = null;
      let lastArch = "";
      let pending: FallenPetal[] = [];
      let globalSlot = 0;
      let trail: TrailEntry[] = [];
      let bloomQueue: BloomJob[] = [];
      let active: BloomJob | null = null;
      let petalAcc = 0;
      // Strand still owed to written words. While it lasts, and until the
      // flowers it started have opened, writing stands in for a voice.
      let writeBudget = 0;
      let writeTail = false;

      function pickArch(): string {
        let total = 0;
        for (const k in ARCH_WEIGHTS) total += ARCH_WEIGHTS[k];
        for (let tries = 0; tries < 3; tries++) {
          let r = Math.random() * total;
          for (const k in ARCH_WEIGHTS) {
            r -= ARCH_WEIGHTS[k];
            if (r <= 0) {
              if (k !== lastArch) return k;
              break;
            }
          }
        }
        return "genda";
      }

      function queueFlower(strandRef: Strand, slotIndex: number, simT: number): FlowerSpec {
        const name = pickArch();
        lastArch = name;
        const spec = SPECIES[name]();
        const flower = createFlower(spec);
        const s = R0 * flower.size * rand(0.7, 1.05);
        const [cx, cy] = strandRef.slots[slotIndex];
        bloomQueue.push({ flower, cx, cy, s, idx: 0, born: simT, strandRef });
        return spec;
      }

      function scheduleFallenPetals(flower: Flower, cx: number, cy: number, s: number, simT: number) {
        const n = randInt(2, 3);
        for (let k = 0; k < n; k++) {
          const p = pick(flower.poses);
          const a = rand(0, TAU);
          pending.push({
            t: simT + 0.3 + k * 0.4,
            x: cx + Math.cos(a) * s * 1.2 + rand(-0.08, 0.08) * W,
            y: cy + s * (1.4 + k * 1.3),
            theta: a + Math.PI / 2 + rand(-0.6, 0.6),
            len: p.len * s * rand(0.7, 1),
            aspect: p.aspect,
            alpha: 70 - k * 10,
            color: flower.pal(p.tone, 0),
            shape: p.shape!,
          });
        }
      }

      function paintFallen(p: FallenPetal) {
        fillPetal(brush, petalOutline(p.x, p.y, p.theta, 0, p.len, p.len * p.aspect, 0.15, p.shape), p.color, p.alpha, 0.4, 0.4);
      }

      function fadeTail(): boolean {
        if (!trail.length) return false;
        let painted = 0;
        for (const e of trail) {
          if (globalSlot - e.slot <= TAIL_FLOWERS) continue;
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

      function advanceBloom(dt: number, loud: number, onset: boolean, simT: number): boolean {
        if (!active && bloomQueue.length) active = bloomQueue.shift()!;
        if (!active) return false;

        if (loud > GATE) {
          const rate = PETAL_RATE_MIN + (PETAL_RATE_MAX - PETAL_RATE_MIN) * Math.pow(loud, 1.3);
          petalAcc += rate * dt;
          if (onset) petalAcc += 2;
        } else {
          petalAcc = Math.min(petalAcc, 0.5);
        }

        let painted = 0;
        while (petalAcc >= 1 && painted < 4 && active.idx < active.flower.poses.length) {
          petalAcc -= 1;
          paintPetal(brush, active.flower, active.flower.poses[active.idx], active.cx, active.cy, active.s, {
            loud,
            accent: onset && painted === 0,
          });
          active.idx++;
          painted++;
        }

        if (active.idx >= active.flower.poses.length) {
          paintCore(brush, active.flower, active.cx, active.cy, active.s);
          globalSlot++;
          trail.push({ cx: active.cx, cy: active.cy + active.s * 0.5, rx: active.s * 1.6, ry: active.s * 2.1, slot: globalSlot, fade: 0 });
          scheduleFallenPetals(active.flower, active.cx, active.cy, active.s, simT);
          active.strandRef.completedSlots++;
          active = null;
          petalAcc = 0;
          return true;
        }
        return painted > 0;
      }

      let last = performance.now();
      let fadeAcc = 0;
      let simT = 0;
      // When something last drew on the garden, washes aside. The washes
      // fade the garden toward paper; once they've had time to fade it all
      // the way, they change nothing, so they stop, and with nothing drawn
      // the canvas is left alone entirely until the garland grows again.
      let lastInk = 0;
      const SETTLE_S = 110;

      function frame(nowMs: number) {
        const dt = Math.min((nowMs - last) / 1000, 0.05);
        last = nowMs;
        simT += dt;
        fadeAcc += dt;

        const onset = A.analyze(dt, simT);

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

        if (writeTail && !A.state.active && writeBudget <= 0 && !active && !bloomQueue.length) writeTail = false;
        const writing = writeTail && !A.state.active;
        const loud = writing ? WRITE_LOUD : A.state.loud;
        const voiced = (A.state.active || writing) && loud > GATE;
        if (voiced) {
          if (!strand || strandDone(strand)) strand = buildStrand(W, H, R0);

          let delta = (GROWTH_MIN + (GROWTH_MAX - GROWTH_MIN) * Math.pow(loud, 1.3)) * dt;
          if (onset && !writing) delta += ONSET_BURST;
          if (writing) delta = Math.min(delta, writeBudget);
          delta = Math.max(0, Math.min(delta, growthCap(strand) - strand.grownDist));
          if (writing) writeBudget -= delta;
          const crossed = growStrand(strand, delta);
          drawStrandGrowth(brush, strand, THREAD, Math.max(1.5, R0 * 0.045));
          dirty = true;
          lastInk = simT;

          for (const i of crossed) {
            const spec = queueFlower(strand, i, simT);
            if (i > 0 && (i + strand.leafPhase) % 3 === 0 && spec.leaf !== "none") {
              const prev = strand.slots[i - 1];
              const pos = strand.slots[i];
              paintLeaf(brush, (prev[0] + pos[0]) / 2, (prev[1] + pos[1]) / 2 + R0 * 0.2, strand.green, spec.leaf, R0);
            }
          }
        }

        const bloomed = advanceBloom(dt, loud, onset && !writing, simT);
        const tailed = fadeTail();
        let fell = false;
        if (pending.length) {
          const due = pending.filter((p) => p.t <= simT);
          if (due.length) {
            pending = pending.filter((p) => p.t > simT);
            for (const p of due) paintFallen(p);
            fell = true;
          }
        }
        if (bloomed || tailed || fell) {
          dirty = true;
          lastInk = simT;
        }

        brush.pop();
        if (dirty) brush.render();
        rafId = requestAnimationFrame(frame);
      }
      rafId = requestAnimationFrame(frame);

      // While huvu talks, a real mic listener runs alongside — the moment
      // it hears actual speech, huvu's reply cuts off immediately. This is
      // the one piece a real voice library (Pipecat/LiveKit) would give
      // for free; done by hand here since there's no such backend yet.
      let stopBargeIn: (() => void) | undefined;

      const onSpeak = () => {
        stopBargeIn?.();
        stopBargeIn = undefined;
        // A cut-off clip still fires onended later, so each utterance
        // announces its end once.
        let spoke = false;
        const announceSpoke = () => {
          if (spoke) return;
          spoke = true;
          window.dispatchEvent(new CustomEvent(HUVU_SPOKE_EVENT));
        };
        A.playFromUrl(TEST_VOICE_URL, {
          onEnded: () => {
            stopBargeIn?.();
            stopBargeIn = undefined;
            announceSpoke();
          },
        })
          .then(() =>
            listenForBargeIn(() => {
              A.disconnectSource();
              stopBargeIn = undefined;
              announceSpoke();
            }),
          )
          .then((stop) => {
            stopBargeIn = stop;
          })
          .catch((err) => {
            console.error("garden canvas: audio failed", err);
            announceSpoke();
          });
      };
      window.addEventListener(HUVU_SPEAK_EVENT, onSpeak);

      const onWrote = (e: Event) => {
        const words = (e as CustomEvent<{ words: number }>).detail?.words ?? 0;
        writeBudget += words * WRITE_PER_WORD;
        writeTail = writeBudget > 0 || writeTail;
      };
      window.addEventListener(HUVU_WROTE_EVENT, onWrote);

      const canvasEl = container.querySelector("canvas");
      cleanup = () => {
        cancelAnimationFrame(rafId);
        window.removeEventListener(HUVU_SPEAK_EVENT, onSpeak);
        window.removeEventListener(HUVU_WROTE_EVENT, onWrote);
        stopBargeIn?.();
        A.disconnectSource();
        canvasEl?.remove();
      };
    })();

    return () => {
      cancelled = true;
      cleanup?.();
      cancelAnimationFrame(rafId);
    };
  }, []);

  return (
    // Behind the chat, at the lowest explicit z-index. A negative z-index
    // looked like the "right" way to force this under normal content, but
    // #root is a flex container and flex ITEMS respect z-index even
    // without `position` set (a real CSS quirk) — so Chat's thread div was
    // an implicit stacking participant too, and the negative value ended
    // up hidden behind body's own background instead of under it.
    // Explicit, non-negative z-index on every layer (this one lowest, the
    // chat/UI layers higher) avoids relying on that quirk at all.
    <div ref={containerRef} className="fixed inset-0 z-0 [&>canvas]:block" />
  );
}
