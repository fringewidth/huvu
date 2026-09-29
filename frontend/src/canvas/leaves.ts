// Leaves along the garland thread. A leaf is just a petal shape at leaf
// proportions, so this reuses petalOutline/fillPetal from the generator
// rather than defining its own geometry.
import { fillPetal, hsl, petalOutline, rand } from "./flowerGenerator";
import type { Brush, LeafType, PetalShape } from "./types";

// Keyed by the `leaf` field a flower spec names. 'none' means the species
// doesn't get a leaf dropped next to it on the strand.
const LEAF_SHAPES: Partial<Record<LeafType, PetalShape & { aspect: number }>> = {
  oval: { skew: 1.0, tip: 1.0, taper: 0.1, ruffle: 0, ruffleFreq: 3, aspect: 0.34 },
  long: { skew: 1.3, tip: 1.2, taper: 0.05, ruffle: 0, ruffleFreq: 3, aspect: 0.22 },
  lobed: { skew: 0.9, tip: 0.7, taper: 0.05, ruffle: 0.25, ruffleFreq: 4, aspect: 0.4 },
  serrate: { skew: 1.05, tip: 1.1, taper: 0.1, ruffle: 0.12, ruffleFreq: 10, aspect: 0.3 },
  pad: { skew: 1.0, tip: 0.6, taper: 0, ruffle: 0.05, ruffleFreq: 8, aspect: 0.9 },
};

export type Green = (d: number) => string;

export function makeGreen(): Green {
  const h = rand(92, 135), s = rand(0.35, 0.55), l = rand(0.42, 0.56);
  return (d) => hsl(h, s, l - 0.08 * d);
}

export function paintLeaf(brush: Brush, x: number, y: number, green: Green, type: LeafType, sizeR: number): void {
  const shape = LEAF_SHAPES[type];
  if (!shape) return;
  const theta = Math.PI / 2 + rand(-0.35, 0.35);
  const len = sizeR * rand(1.4, 2.0);
  const wid = len * shape.aspect;
  const curl = rand(-0.15, 0.15);
  fillPetal(brush, petalOutline(x, y, theta, 0, len, wid, curl, shape), green(rand(0, 0.4)), 60 + rand(0, 25), 0.3, 0.45);
  fillPetal(brush, petalOutline(x, y, theta, 0, len * 0.92, len * 0.05, 0, shape), green(0.7), 50, 0.1, 0.2);
}
