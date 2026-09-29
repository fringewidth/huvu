// Genda (marigold) — ported from the recovered prototype's archetype.
// Placeholder species: a working default so the garland has something to
// bloom, not a final creative decision. To be redirected once the owner
// art-directs the real species.
import { pick, rand } from "../flowerGenerator";
import type { FlowerSpec } from "../types";

export default function genda(): FlowerSpec {
  const shape = { skew: rand(0.9, 1.1), tip: rand(0.45, 0.7), taper: 0.1, ruffle: rand(0.15, 0.3), ruffleFreq: pick([5, 7]) };
  const phase = rand(0, Math.PI * 2);
  return {
    name: "genda",
    size: 1,
    shape,
    palette: { hMin: 24, hMax: 48, sMin: 0.85, sMax: 1, lOuter: 0.66, lInner: 0.5, spread: 14 },
    whorls: [
      { kind: "ring", count: 9, phase, base: 0.12, len: 0.62, aspect: rand(0.75, 0.95), tone: 0.1 },
      { kind: "ring", count: 8, phase: phase + 0.35, base: 0.08, len: 0.48, aspect: 0.85, tone: 0.5 },
      { kind: "ring", count: 6, phase: phase + 0.7, base: 0.04, len: 0.34, aspect: 0.9, tone: 0.9 },
    ],
    core: "dot-dark",
    leaf: "lobed",
  };
}
