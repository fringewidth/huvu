// The garland's connecting string. One continuous path, traced forward as
// audio noise drives it — see growStrand() for the growth math and
// drawStrandGrowth() for how the newly-grown bit gets painted.
import { pick, randInt, rand } from "./flowerGenerator";
import { makeGreen, type Green } from "./leaves";
import type { Brush, Point } from "./types";

export interface Strand {
  path: Point[];
  dist: number[];
  total: number;
  slots: Point[];
  slotDist: number[];
  nextSlot: number;
  grownDist: number;
  tracedCount: number;
  leafPhase: number;
  green: Green;
  // How many slots' flowers have fully finished blooming (incremented by
  // the caller). Growth is capped to this — see growthCap().
  completedSlots: number;
}

function catmullRom(ctrl: Point[], perSeg: number): Point[] {
  const pts: Point[] = [];
  for (let i = 0; i < ctrl.length - 1; i++) {
    const p0 = ctrl[Math.max(0, i - 1)], p1 = ctrl[i], p2 = ctrl[i + 1], p3 = ctrl[Math.min(ctrl.length - 1, i + 2)];
    for (let k = 0; k < perSeg; k++) {
      const t = k / perSeg, t2 = t * t, t3 = t2 * t;
      pts.push([
        0.5 * (2 * p1[0] + (-p0[0] + p2[0]) * t + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3),
        0.5 * (2 * p1[1] + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3),
      ]);
    }
  }
  pts.push(ctrl[ctrl.length - 1]);
  return pts;
}

function edgePoint(side: "left" | "right" | "top" | "bottom", W: number, H: number): Point {
  const m = 0.06;
  if (side === "left") return [W * m, H * rand(0.15, 0.85)];
  if (side === "right") return [W * (1 - m), H * rand(0.15, 0.85)];
  if (side === "top") return [W * rand(0.15, 0.85), H * 0.08];
  return [W * rand(0.15, 0.85), H * 0.9];
}

// Builds a fresh strand: a Catmull-Rom path between two random screen
// edges, with flower slots spaced along it. Growth/slot-crossing state
// starts at zero — nothing is drawn until growStrand() advances it.
export function buildStrand(W: number, H: number, R0: number): Strand {
  const sides: Array<"left" | "right" | "top" | "bottom"> = ["left", "right", "top", "bottom"];
  const s0 = pick(sides);
  const s1 = pick(sides.filter((s) => s !== s0));
  const start = edgePoint(s0, W, H), end = edgePoint(s1, W, H);
  const dir: Point = [end[0] - start[0], end[1] - start[1]];
  const mids: Point[] = [];
  for (let k = 0; k < randInt(2, 4); k++) mids.push([W * rand(0.12, 0.88), H * rand(0.12, 0.82)]);
  mids.sort((a, b) => a[0] * dir[0] + a[1] * dir[1] - (b[0] * dir[0] + b[1] * dir[1]));
  const path = catmullRom([start, ...mids, end], 60);

  const dist = [0];
  for (let k = 1; k < path.length; k++) {
    dist.push(dist[k - 1] + Math.hypot(path[k][0] - path[k - 1][0], path[k][1] - path[k - 1][1]));
  }
  const total = dist[dist.length - 1];

  const spacing = R0 * 1.55;
  const slots: Point[] = [];
  const slotDist: number[] = [];
  let next = spacing * 0.6;
  for (let k = 1; k < path.length; k++) {
    if (dist[k] >= next) {
      slots.push(path[k]);
      slotDist.push(dist[k]);
      next += spacing;
    }
  }

  return {
    path,
    dist,
    total,
    slots,
    slotDist,
    nextSlot: 0,
    grownDist: 0,
    tracedCount: 2,
    leafPhase: randInt(0, 2),
    green: makeGreen(),
    completedSlots: 0,
  };
}

// The furthest the string is allowed to grow this frame: up to the next
// slot that hasn't finished blooming yet, not beyond. Once that flower
// completes (strand.completedSlots increments), the cap moves to the slot
// after it. Without this, a loud voice can grow the whole string across
// the screen in a couple of seconds while only the first flower or two
// have had time to actually bloom.
export function growthCap(strand: Strand): number {
  return strand.completedSlots < strand.slots.length ? strand.slotDist[strand.completedSlots] : strand.total;
}

// Advances how much of the strand has grown, by `deltaDist` (px along the
// path) — the caller derives this from audio loudness/onsets each frame.
// Returns the indices of any flower slots just grown past, for the caller
// to spawn a flower (and maybe a leaf) at.
export function growStrand(strand: Strand, deltaDist: number): number[] {
  strand.grownDist = Math.min(strand.total, strand.grownDist + deltaDist);
  const crossed: number[] = [];
  while (strand.nextSlot < strand.slots.length && strand.slotDist[strand.nextSlot] <= strand.grownDist) {
    crossed.push(strand.nextSlot);
    strand.nextSlot++;
  }
  return crossed;
}

export function strandDone(strand: Strand): boolean {
  return strand.grownDist >= strand.total;
}

// Paints only the newly-grown segment of the string as a filled ribbon,
// using the same watercolor fill/bleed/texture as petals — a painted
// strip, not a technical pen line. Drawing just the increment (not the
// whole prefix again) keeps growth reading as growth: each frame adds one
// new brushstroke sized by how much louder the sound was, rather than
// every frame re-touching everything already drawn.
export function drawStrandGrowth(brush: Brush, strand: Strand, threadColor: string, halfWidth: number): void {
  const from = strand.tracedCount;
  let to = from;
  while (to < strand.path.length && strand.dist[to] < strand.grownDist) to++;
  if (to <= from) return;

  const startIdx = Math.max(0, from - 1);
  const segment = strand.path.slice(startIdx, to + 1);
  if (segment.length < 2) {
    strand.tracedCount = to;
    return;
  }

  const left: Point[] = [];
  const right: Point[] = [];
  for (let k = 0; k < segment.length; k++) {
    const p = segment[k];
    const prev = segment[Math.max(0, k - 1)];
    const next = segment[Math.min(segment.length - 1, k + 1)];
    let nx = -(next[1] - prev[1]);
    let ny = next[0] - prev[0];
    const nl = Math.hypot(nx, ny) || 1;
    nx = (nx / nl) * halfWidth;
    ny = (ny / nl) * halfWidth;
    left.push([p[0] + nx, p[1] + ny]);
    right.push([p[0] - nx, p[1] - ny]);
  }
  brush.fill(threadColor, 130);
  brush.fillBleed(0.15, "out");
  brush.fillTexture(0.3, 0.4);
  brush.polygon(left.concat(right.reverse()));
  strand.tracedCount = to;
}
