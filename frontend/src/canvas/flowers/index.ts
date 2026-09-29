import type { FlowerSpec } from "../types";
import champa from "./champa";
import genda from "./genda";
import gudhal from "./gudhal";
import gulab from "./gulab";
import kamal from "./kamal";
import kaner from "./kaner";
import mogra from "./mogra";
import palash from "./palash";
import rugmini from "./rugmini";

// Placeholder species, until the owner art-directs the real ones. A
// higher weight blooms more often.
const ROSTER: { make: () => FlowerSpec; weight: number }[] = [
  { make: genda, weight: 5 },
  { make: mogra, weight: 3 },
  { make: gulab, weight: 3 },
  { make: gudhal, weight: 2 },
  { make: champa, weight: 1.5 },
  { make: kaner, weight: 1.5 },
  { make: rugmini, weight: 1.5 },
  { make: kamal, weight: 1.2 },
  { make: palash, weight: 0.8 },
];

let last: (typeof ROSTER)[number] | null = null;

// A weighted pick that never repeats the previous species.
export function nextSpecies(): FlowerSpec {
  const choices = ROSTER.filter((s) => s !== last);
  let r = Math.random() * choices.reduce((sum, s) => sum + s.weight, 0);
  last = choices.find((s) => (r -= s.weight) <= 0) ?? choices[0];
  return last.make();
}
