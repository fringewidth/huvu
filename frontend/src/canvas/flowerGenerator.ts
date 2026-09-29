// Flower generator core: turns a declarative spec (whorls, petal shape,
// palette) into a flat list of petal poses, plus the fill routines used to
// actually paint one. One file per real flower species lives in flowers/
// and just describes a spec — this module is the only place that knows
// how to resolve and draw one.
import { floretDisc, floretRing } from "./cluster";
import type {
  Brush,
  ClusterDiscWhorl,
  ClusterRingWhorl,
  CoreType,
  FanWhorl,
  Flower,
  FlowerSpec,
  Palette,
  PaletteSpec,
  PetalShape,
  Point,
  Pose,
  RingWhorl,
  Whorl,
} from "./types";

export const TAU = Math.PI * 2;

export const rand = (a: number, b: number) => a + Math.random() * (b - a);
export const randInt = (a: number, b: number) => Math.floor(rand(a, b + 1));
export function pick<T>(arr: T[]): T {
  return arr[Math.floor(Math.random() * arr.length)];
}

export function hsl(h: number, s: number, l: number): string {
  h = ((h % 360) + 360) % 360;
  return `hsl(${h.toFixed(1)}, ${(Math.min(1, Math.max(0, s)) * 100).toFixed(1)}%, ${(Math.min(0.97, Math.max(0.3, l)) * 100).toFixed(1)}%)`;
}

// A palette maps petal tone (0 = outer edge, 1 = center) to a color.
// `nudge` shifts hue slightly; kept for later audio-reactive use.
export function makePalette({ hMin, hMax, sMin, sMax, lOuter, lInner, spread }: PaletteSpec): Palette {
  const h = rand(hMin, hMax);
  const s = rand(sMin, sMax);
  return (tone, nudge = 0) => hsl(h + (tone - 0.5) * spread + nudge * 10, s, lOuter + (lInner - lOuter) * tone);
}

// Whorls resolve into a flat list of petal poses. "ring" (default) —
// evenly spaced around a full circle, starting at `phase`. "fan" — spread
// across an arc `span` wide, centered on `center`.
function resolveRing(whorl: RingWhorl): Pose[] {
  const { count, phase = 0, ...rest } = whorl;
  const poses: Pose[] = [];
  for (let k = 0; k < count; k++) {
    poses.push({ ...rest, theta: phase + (k * TAU) / count });
  }
  return poses;
}

function resolveFan(whorl: FanWhorl): Pose[] {
  const { count, center, span, cup = 0, curl = 0, ...rest } = whorl;
  const poses: Pose[] = [];
  for (let k = 0; k < count; k++) {
    const u = count > 1 ? k / (count - 1) - 0.5 : 0;
    poses.push({ ...rest, theta: center + u * span, curl: cup ? -u * cup : curl });
  }
  return poses;
}

// "cluster" — scattered florets (each its own small ring of petals). The
// scatter math lives in cluster.ts since it's a placement pattern, not
// petal geometry.
const CLUSTER_RESOLVERS = {
  ring: floretRing as (w: ClusterRingWhorl | ClusterDiscWhorl) => Pose[],
  disc: floretDisc as (w: ClusterRingWhorl | ClusterDiscWhorl) => Pose[],
};

export function resolveWhorls(whorls: Whorl[]): Pose[] {
  const poses: Pose[] = [];
  whorls.forEach((whorl, whorlIndex) => {
    let resolved: Pose[];
    if (whorl.kind === "fan") resolved = resolveFan(whorl);
    else if (whorl.kind === "cluster") resolved = CLUSTER_RESOLVERS[whorl.clusterKind ?? "ring"](whorl);
    else resolved = resolveRing(whorl);
    for (const p of resolved) poses.push({ ...p, whorlIndex });
  });
  return poses;
}

// Builds a flower instance from a declarative spec. `shape` may be a
// single shape object (applied to every petal) or an array with one shape
// per whorl, so a species can e.g. make its inner whorl pointier than its
// outer one.
export function createFlower(spec: FlowerSpec): Flower {
  const poses = resolveWhorls(spec.whorls);
  const shapes = Array.isArray(spec.shape) ? spec.shape : null;
  for (const p of poses) {
    p.shape = shapes ? shapes[p.whorlIndex ?? 0] : (spec.shape as PetalShape);
  }
  return {
    name: spec.name,
    pal: makePalette(spec.palette),
    core: spec.core ?? "none",
    size: spec.size ?? 1,
    poses,
  };
}

// ---------- petal geometry ----------
// Full closed outline of a petal: base -> tip along one edge, tip -> base
// along the other. Shape params: skew (where the widest point sits), tip
// (pointiness), taper, ruffle/ruffleFreq (edge waviness).
export function petalOutline(
  cx: number,
  cy: number,
  theta: number,
  base: number,
  len: number,
  wid: number,
  curl: number,
  shape: PetalShape,
  n = 10,
): Point[] {
  const pts: Point[] = [];
  const side = (s: number, sign: number) => {
    const ss = Math.pow(s, shape.skew);
    let w = Math.pow(Math.sin(Math.PI * ss), shape.tip) * (1 - shape.taper * s) + 0.05 * (1 - s);
    if (shape.ruffle) w *= 1 + shape.ruffle * Math.sin(s * shape.ruffleFreq * Math.PI);
    const along = base + s * len;
    const across = sign * w * wid + curl * len * s * s;
    pts.push([
      cx + Math.cos(theta) * along - Math.sin(theta) * across,
      cy + Math.sin(theta) * along + Math.cos(theta) * across,
    ]);
  };
  for (let k = 0; k <= n; k++) side(k / n, 1);
  for (let k = n - 1; k > 0; k--) side(k / n, -1);
  return pts;
}

// ---------- petal fill ----------
// Petals paint in as an instant watercolor fill, one after another. Growth
// (the spline technique) is reserved for the garland string, not petals.
export function fillPetal(brush: Brush, outline: Point[], color: string, alpha: number, bleed: number, texture: number): void {
  brush.fill(color, alpha);
  brush.fillBleed(bleed, "out");
  brush.fillTexture(texture, 0.5);
  brush.polygon(outline);
}

// Paints one petal, including the depth cues that keep petals from reading
// as flat wedges: an optional drop shadow (pose.shadow, for florets
// scattered off-center), and either a two-tone inner petal (pose.twoTone)
// or a brighter inner highlight layer for any petal long enough to carry
// one. `loud` is 0..1, the current audio level — 1 for a static render.
export function paintPetal(
  brush: Brush,
  flower: Flower,
  pose: Pose,
  cx0: number,
  cy0: number,
  s: number,
  { loud = 1, nudge = 0, accent = false }: { loud?: number; nudge?: number; accent?: boolean } = {},
): void {
  const shape = pose.shape!;
  const len = pose.len * s * (0.75 + 0.5 * loud);
  const wid = len * pose.aspect;
  const cx = cx0 + (pose.dx || 0) * s;
  const cy = cy0 + (pose.dy || 0) * s;
  const curl = pose.curl || 0;
  const alpha = (accent ? 90 : 55) + 70 * loud;

  if (pose.shadow) {
    fillPetal(
      brush,
      petalOutline(cx + s * 0.03, cy + s * 0.04, pose.theta, pose.base * s, len, wid, curl, shape),
      hsl(280, 0.15, 0.78),
      45,
      0.3,
      0.3,
    );
  }
  fillPetal(
    brush,
    petalOutline(cx, cy, pose.theta, pose.base * s, len, wid, curl, shape),
    flower.pal(pose.tone, nudge),
    alpha,
    0.25 + 0.4 * loud,
    0.3 + 0.3 * loud,
  );
  if (pose.twoTone) {
    fillPetal(
      brush,
      petalOutline(cx, cy, pose.theta, pose.base * s, len * 0.45, wid * 0.7, curl, shape),
      flower.pal(1, nudge),
      70,
      0.3,
      0.3,
    );
  } else if (!pose.shadow && pose.len > 0.45) {
    fillPetal(
      brush,
      petalOutline(cx, cy, pose.theta, pose.base * s, len * 0.5, wid * 0.55, curl, shape),
      flower.pal(Math.min(1, pose.tone + 0.35), nudge),
      28 + 30 * loud,
      0.2,
      0.25,
    );
  }
}

// ---------- core ----------
function circlePts(x: number, y: number, r: number): Point[] {
  const pts: Point[] = [];
  for (let k = 0; k < 14; k++) {
    const a = (k / 14) * TAU;
    const rr = r * (1 + 0.06 * Math.sin(a * 3 + x));
    pts.push([x + Math.cos(a) * rr, y + Math.sin(a) * rr]);
  }
  return pts;
}

// Paints the flower's center, keyed by `flower.core`. Shared across
// species — a per-flower spec just names one of these types rather than
// each flower file reimplementing its own center.
export function paintCore(brush: Brush, flower: Flower, cx: number, cy: number, s: number): void {
  const c: CoreType = flower.core;
  if (!c || c === "none") return;
  if (c === "dot-dark") {
    fillPetal(brush, circlePts(cx, cy, s * 0.12), hsl(20, 0.7, 0.32), 80, 0.2, 0.5);
  } else if (c === "dot-yellow") {
    fillPetal(brush, circlePts(cx, cy, s * 0.16), hsl(45, 0.9, 0.6), 90, 0.25, 0.5);
  } else if (c === "dot-tiny") {
    fillPetal(brush, circlePts(cx, cy, s * 0.06), hsl(60, 0.6, 0.7), 90, 0.2, 0.3);
  } else if (c === "lotus") {
    fillPetal(brush, circlePts(cx, cy - s * 0.25, s * 0.28), hsl(48, 0.9, 0.62), 85, 0.3, 0.5);
    for (let k = 0; k < 7; k++) {
      const a = rand(0, TAU), d = rand(0, s * 0.22);
      fillPetal(brush, circlePts(cx + Math.cos(a) * d, cy - s * 0.25 + Math.sin(a) * d, s * 0.035), hsl(35, 0.9, 0.45), 90, 0.1, 0.2);
    }
  } else if (c === "stamen") {
    const a = -Math.PI / 2 + rand(-0.5, 0.5);
    const L = s * 1.25;
    const x1 = cx + Math.cos(a) * L, y1 = cy + Math.sin(a) * L;
    fillPetal(brush, [[cx - 2, cy], [x1 - 1.5, y1], [x1 + 1.5, y1], [cx + 2, cy]], flower.pal(1, 0), 120, 0.1, 0.2);
    fillPetal(brush, circlePts(cx, cy, s * 0.14), hsl(350, 0.7, 0.3), 80, 0.2, 0.5);
    for (let k = 0; k < 6; k++) {
      fillPetal(brush, circlePts(x1 + rand(-0.12, 0.12) * s, y1 + rand(-0.12, 0.12) * s, s * 0.04), hsl(48, 0.95, 0.6), 100, 0.1, 0.2);
    }
  } else if (c === "calyx") {
    fillPetal(brush, circlePts(cx, cy + s * 0.15, s * 0.16), hsl(30, 0.2, 0.35), 85, 0.2, 0.5);
  }
}
