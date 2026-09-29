// Champa (plumeria).
import { pick, rand } from "../util";
import type { FlowerSpec } from "../types";

export default function champa(): FlowerSpec {
  const shape = { skew: rand(1.35, 1.6), tip: rand(0.6, 0.8), taper: 0, ruffle: 0, ruffleFreq: 3 };
  const twist = pick([-1, 1]) * rand(0.25, 0.4);
  return {
    shape,
    palette: { hMin: 40, hMax: 55, sMin: 0.7, sMax: 0.9, lOuter: 0.93, lInner: 0.7, spread: 4 },
    whorls: [
      { kind: "ring", count: 5, phase: rand(0, Math.PI * 2), base: 0.02, len: 1.0, aspect: rand(0.55, 0.65), tone: 0.05, curl: twist, twoTone: true },
    ],
    core: "dot-yellow",
    leaf: "long",
  };
}
