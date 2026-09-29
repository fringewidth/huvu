// Text that moves between two places instead of vanishing from one and
// appearing in the other: your typed ask flies from the composer up into
// the YOU cue, and an edited ask flies back down. The source takes a
// snapshot and launches; the destination lands the flight once it has
// mounted. In between, a ghost copy of the text travels on top of
// everything while the real destination stays hidden.
//
// The ghost moves by transform only (translate and scale), so the flight
// costs no layout, and it re-measures the destination every frame, so it
// still lands true if the destination moves mid-flight.
export interface Snapshot {
  left: number;
  top: number;
  width: number;
  fontSize: number;
  lineHeight: number;
  color: string;
}

const FLIGHT_MS = 750;
// A launch nobody lands within this window is stale.
const STALE_MS = 3000;

let pending: { key: string; text: string; from: Snapshot; at: number } | null = null;

// cubic-bezier(.2, .8, .2, 1), close enough, as a plain function.
function ease(t: number): number {
  return 1 - Math.pow(1 - t, 3.2);
}

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

// Measures where an element's text starts. For a textarea that is inside
// its padding, not at its border.
export function snapshot(el: HTMLElement): Snapshot {
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  const padL = parseFloat(cs.paddingLeft);
  const padT = parseFloat(cs.paddingTop);
  const fontSize = parseFloat(cs.fontSize);
  const lh = parseFloat(cs.lineHeight);
  return {
    left: r.left + padL,
    top: r.top + padT,
    width: r.width - padL - parseFloat(cs.paddingRight),
    fontSize,
    lineHeight: Number.isFinite(lh) ? lh : fontSize * 1.3,
    color: cs.color,
  };
}

export function launch(key: string, text: string, from: Snapshot): void {
  pending = { key, text, from, at: performance.now() };
}

// Call from a layout effect once `el` shows `text`. Does nothing unless a
// matching flight is waiting.
export function land(key: string, text: string, el: HTMLElement): void {
  const flight = pending;
  if (!flight || flight.key !== key || flight.text !== text) return;
  pending = null;
  if (performance.now() - flight.at > STALE_MS) return;
  // With reduced motion the text simply appears where it belongs.
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  const { from } = flight;
  const to = snapshot(el);
  const cs = getComputedStyle(el);
  // The ghost is set at the source size and scaled down to the
  // destination size. Its width is chosen so that, once scaled, it wraps
  // exactly like the destination, and the hand-over at the end is seamless.
  const endScale = to.fontSize / from.fontSize;
  const ghost = document.createElement("div");
  ghost.className = "halo";
  ghost.textContent = text;
  Object.assign(ghost.style, {
    position: "fixed",
    left: "0",
    top: "0",
    zIndex: "50",
    pointerEvents: "none",
    margin: "0",
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
    transformOrigin: "0 0",
    willChange: "transform",
    fontFamily: cs.fontFamily,
    fontWeight: cs.fontWeight,
    letterSpacing: cs.letterSpacing,
    fontSize: `${from.fontSize}px`,
    lineHeight: `${(to.lineHeight / to.fontSize) * from.fontSize}px`,
    width: `${to.width / endScale}px`,
    color: from.color,
    transition: `color ${FLIGHT_MS}ms`,
    transform: `translate(${from.left}px, ${from.top}px)`,
  });
  document.body.appendChild(ghost);
  const hidden = el.style.visibility;
  el.style.visibility = "hidden";
  requestAnimationFrame(() => (ghost.style.color = to.color));

  // The live position is read from the border box each frame, so keep the
  // padding offset the snapshot measured.
  const box = el.getBoundingClientRect();
  const offX = to.left - box.left;
  const offY = to.top - box.top;
  const start = performance.now();
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    el.style.visibility = hidden;
    ghost.remove();
  };
  const step = (now: number) => {
    if (done) return;
    const t = Math.min(1, (now - start) / FLIGHT_MS);
    const e = ease(t);
    const r = el.getBoundingClientRect();
    const x = lerp(from.left, r.left + offX, e);
    const y = lerp(from.top, r.top + offY, e);
    ghost.style.transform = `translate(${x}px, ${y}px) scale(${lerp(1, endScale, e)})`;
    if (t < 1) requestAnimationFrame(step);
    else finish();
  };
  requestAnimationFrame(step);
  // Frames stop in a background tab. Never leave the destination hidden.
  window.setTimeout(finish, FLIGHT_MS + 500);
}
