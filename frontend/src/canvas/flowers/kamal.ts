// Kamal (lotus).
import { rand } from "../util";
import type { FlowerSpec } from "../types";

export default function kamal(): FlowerSpec {
  const shape = { skew: rand(1.0, 1.2), tip: rand(1.2, 1.6), taper: 0.15, ruffle: 0, ruffleFreq: 3 };
  return {
    shape,
    palette: { hMin: 330, hMax: 350, sMin: 0.6, sMax: 0.8, lOuter: 0.86, lInner: 0.68, spread: 8 },
    whorls: [
      { kind: "fan", count: 6, center: -Math.PI / 2, span: rand(2.3, 2.8), base: 0, len: 1.35, aspect: rand(0.3, 0.38), tone: 0.15, cup: 0.55 },
      { kind: "fan", count: 4, center: -Math.PI / 2, span: rand(1.3, 1.6), base: 0.05, len: 1.05, aspect: 0.34, tone: 0.75, cup: 0.4 },
    ],
    core: "lotus",
    leaf: "pad",
  };
}
