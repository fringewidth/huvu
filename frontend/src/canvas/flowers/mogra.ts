// Mogra (jasmine) — ported placeholder, see genda.ts for context. Uses
// cluster whorls: several small six-petal florets scattered around the
// slot, rather than one ring of full-size petals.
import { rand, randInt } from "../flowerGenerator";
import type { FlowerSpec } from "../types";

export default function mogra(): FlowerSpec {
  const shape = { skew: rand(1.0, 1.15), tip: rand(0.7, 0.95), taper: 0.05, ruffle: 0, ruffleFreq: 3 };
  return {
    name: "mogra",
    size: 1,
    shape,
    palette: { hMin: 270, hMax: 300, sMin: 0.12, sMax: 0.25, lOuter: 0.96, lInner: 0.84, spread: 6 },
    whorls: [
      { kind: "cluster", clusterKind: "ring", count: randInt(3, 5), floretPetals: 6, base: 0.05, len: 0.42, aspect: 0.38, tone: 0.2, offsetMin: 0.6, offsetMax: 1.2, includeCenter: true },
      { kind: "cluster", clusterKind: "ring", count: 2, floretPetals: 6, base: 0.1, len: 0.45, aspect: 0.3, tone: 0.5, offsetMin: 0.9, offsetMax: 1.4, includeCenter: false },
    ],
    core: "dot-tiny",
    leaf: "oval",
  };
}
