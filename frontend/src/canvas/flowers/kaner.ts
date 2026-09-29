// Kaner (oleander).
import { pick, rand } from "../util";
import type { FlowerSpec } from "../types";

export default function kaner(): FlowerSpec {
  const shape = { skew: rand(1.15, 1.35), tip: rand(0.6, 0.8), taper: 0, ruffle: 0, ruffleFreq: 3 };
  const twist = pick([-1, 1]) * rand(0.2, 0.35);
  return {
    shape,
    palette: { hMin: 328, hMax: 346, sMin: 0.6, sMax: 0.8, lOuter: 0.8, lInner: 0.66, spread: 6 },
    whorls: [
      { kind: "ring", count: 5, phase: rand(0, Math.PI * 2), base: 0.05, len: 0.95, aspect: rand(0.5, 0.6), tone: 0.3, curl: twist },
    ],
    core: "dot-yellow",
    leaf: "long",
  };
}
