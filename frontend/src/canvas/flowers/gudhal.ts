// Gudhal (hibiscus).
import { rand } from "../util";
import type { FlowerSpec } from "../types";

export default function gudhal(): FlowerSpec {
  const shape = { skew: rand(1.05, 1.25), tip: rand(0.5, 0.7), taper: 0.05, ruffle: rand(0.08, 0.16), ruffleFreq: 5 };
  return {
    shape,
    palette: { hMin: 350, hMax: 8, sMin: 0.8, sMax: 0.95, lOuter: 0.6, lInner: 0.42, spread: 6 },
    whorls: [
      { kind: "ring", count: 5, phase: rand(0, Math.PI * 2), base: 0.1, len: 1.15, aspect: rand(0.8, 0.95), tone: 0.3, curl: rand(-0.15, 0.15) },
    ],
    core: "stamen",
    leaf: "serrate",
  };
}
