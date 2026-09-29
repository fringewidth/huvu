// Rugmini — ported placeholder, see genda.ts for context. A disc-scattered
// cluster of small four-petal florets, each picking its own tone.
import { rand, randInt } from "../flowerGenerator";
import type { FlowerSpec } from "../types";

export default function rugmini(): FlowerSpec {
  const shape = { skew: rand(1.0, 1.1), tip: rand(1.1, 1.4), taper: 0.1, ruffle: 0, ruffleFreq: 3 };
  return {
    name: "rugmini",
    size: 1,
    shape,
    palette: { hMin: 4, hMax: 20, sMin: 0.8, sMax: 0.95, lOuter: 0.66, lInner: 0.55, spread: 8 },
    whorls: [
      { kind: "cluster", clusterKind: "disc", count: randInt(7, 9), floretPetals: 4, base: 0.03, len: 0.3, aspect: 0.42, radiusX: 0.95, radiusY: 0.76, toneRandom: true },
    ],
    core: "none",
    leaf: "oval",
  };
}
