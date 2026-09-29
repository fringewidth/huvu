// Floret clusters: several small florets, each its own tiny ring of
// petals, scattered around a whorl's center. For species like mogra and
// rugmini, where one "flower" is a bunch of small blossoms.
import type { ClusterDiscWhorl, ClusterRingWhorl, Pose } from "./types";
import { rand, TAU } from "./util";

// One floret at the center (offset 0), the rest scattered at random angles
// within [offsetMin, offsetMax] of it. Marks every petal `shadow: true` so
// paintPetal gives each floret a soft drop shadow, reading as a separate
// bloom rather than a flat extra ring.
export function floretRing(whorl: ClusterRingWhorl): Pose[] {
  const { count, floretPetals, base, len, aspect, tone, offsetMin, offsetMax, includeCenter = true } = whorl;
  const poses: Pose[] = [];
  for (let m = 0; m < count; m++) {
    const a = rand(0, TAU);
    const d = includeCenter && m === 0 ? 0 : rand(offsetMin, offsetMax);
    const dx = Math.cos(a) * d, dy = Math.sin(a) * d;
    const ph = rand(0, TAU);
    for (let k = 0; k < floretPetals; k++) {
      poses.push({
        theta: ph + (k * TAU) / floretPetals,
        base,
        len,
        aspect,
        tone,
        curl: 0,
        dx,
        dy,
        shadow: true,
      });
    }
  }
  return poses;
}

// Uniform scatter across a disc/ellipse (radiusX, radiusY), each floret its
// own small ring. `toneRandom` lets each floret pick its own tone instead
// of sharing one, for a more varied, less concentric look.
export function floretDisc(whorl: ClusterDiscWhorl): Pose[] {
  const { count, floretPetals, base, len, aspect, tone, radiusX, radiusY, toneRandom = false } = whorl;
  const poses: Pose[] = [];
  for (let m = 0; m < count; m++) {
    const a = rand(0, TAU), d = Math.sqrt(Math.random());
    const dx = Math.cos(a) * d * radiusX, dy = Math.sin(a) * d * radiusY;
    const ph = rand(0, TAU);
    for (let k = 0; k < floretPetals; k++) {
      poses.push({
        theta: ph + (k * TAU) / floretPetals,
        base,
        len,
        aspect,
        tone: toneRandom ? rand(0, 1) : tone ?? 0,
        curl: 0,
        dx,
        dy,
      });
    }
  }
  return poses;
}
