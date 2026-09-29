// Palash (flame of the forest).
import { pick, rand } from "../util";
import type { FlowerSpec } from "../types";

export default function palash(): FlowerSpec {
  const shape = { skew: rand(0.85, 1.0), tip: rand(1.0, 1.3), taper: 0.25, ruffle: 0, ruffleFreq: 3 };
  const dir = pick([-1, 1]);
  return {
    shape,
    palette: { hMin: 14, hMax: 30, sMin: 0.9, sMax: 1, lOuter: 0.58, lInner: 0.48, spread: 6 },
    whorls: [
      { kind: "fan", count: 4, center: -Math.PI / 2 + dir * 0.35, span: rand(1.1, 1.5), base: 0.1, len: 1.25, aspect: rand(0.26, 0.32), tone: 0.4, curl: dir * rand(0.45, 0.6) },
    ],
    core: "calyx",
    leaf: "none",
  };
}
