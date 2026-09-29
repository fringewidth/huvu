// Gulab (rose). Four rings sharing one aspect ratio, spiraling inward
// tighter and paler.
import { rand } from "../util";
import type { FlowerSpec } from "../types";

export default function gulab(): FlowerSpec {
  const shape = { skew: rand(1.0, 1.15), tip: rand(0.5, 0.7), taper: 0.05, ruffle: rand(0, 0.08), ruffleFreq: 3 };
  const phase = rand(0, Math.PI * 2);
  const aspect = rand(0.7, 0.9);
  return {
    shape,
    palette: { hMin: 342, hMax: 360, sMin: 0.75, sMax: 0.9, lOuter: 0.62, lInner: 0.42, spread: 6 },
    whorls: [
      { kind: "ring", count: 6, phase, base: 0.12, len: 1.0, aspect, tone: 0.05, curl: rand(-0.25, 0.25) },
      { kind: "ring", count: 5, phase: phase + 0.5, base: 0.08, len: 0.74, aspect, tone: 0.4, curl: rand(-0.25, 0.25) },
      { kind: "ring", count: 4, phase: phase + 1.1, base: 0.04, len: 0.5, aspect, tone: 0.75, curl: rand(-0.2, 0.2) },
      { kind: "ring", count: 3, phase: phase + 1.7, base: 0.02, len: 0.32, aspect, tone: 1, curl: 0.15 },
    ],
    core: "none",
    leaf: "serrate",
  };
}
