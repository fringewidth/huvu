import { memo, useEffect, useId } from "react";
import * as m from "motion/react-m";
import { useMotionValue, useSpring, useTransform, type MotionValue, type TargetAndTransition, type Transition } from "motion/react";
import { BLUSH, IVORY, MARIGOLD, THREAD } from "../config/palette";
import { EASE } from "../stage/style";

// The flower is voice mode's stand-in for huvu. Closed to a bud while you
// type; open and dancing once voice mode is on. While it listens it swells
// gently with your voice (`level`, 0 to 1). Huvu's own voice goes to the
// garland behind, not to the flower. While huvu works (before it speaks,
// and while it runs tools) the flower half closes, turns, and a sindoor
// halo breathes behind it. By the text field, the bud turns the same way
// while a reply comes in. Where the flower sits on screen is the
// parent's job; this component only draws and animates it in place.
export type FlowerState = "bud" | "dancing" | "listening" | "thinking" | "speaking";

interface VoiceFlowerProps {
  state: FlowerState;
  // Turns the flower while huvu works, whatever the state.
  spinning: boolean;
  size: number;
  level: MotionValue<number>;
  label: string;
  onClick: () => void;
}

const PETAL = "M0,0 C-20,-6 -19,-36 -3,-45 C13,-41 12,-11 0,0Z";
const RING = [0, 72, 144, 216, 288];

const SWAY = { rotate: [0, -6, 6, 0], scale: [1, 0.97, 1.03, 1] };
const STILL = { rotate: 0, scale: 1 };
const swayFor = (seconds: number): Transition => ({ duration: seconds, repeat: Infinity, ease: "easeInOut" });
// The listening echo: a ring that fades in as it leaves the flower and
// out as it grows, so it starts and ends unseen and never pops in.
const ECHO_SECONDS = 2.4;
const ECHO: Transition = {
  scale: { duration: ECHO_SECONDS, repeat: Infinity, ease: "easeOut" },
  opacity: { duration: ECHO_SECONDS, repeat: Infinity, times: [0, 0.15, 1], ease: ["easeOut", "easeIn"] },
};

// Working turns the flower this many degrees a second. When it stops,
// a spring carries it on to the next petal, so it comes to rest the way
// it started: five petals look the same every 72 degrees.
const SPIN_SPEED = 60;
const PETAL_TURN = 72;
// Thinking's halo, breathing in and out.
const HALO: Transition = { duration: 2.4, repeat: Infinity, ease: "easeInOut" };

// How the petals move in each state. Changing state tweens from wherever
// the petals are, so a switch never snaps.
const MOTION: Record<FlowerState, { animate: TargetAndTransition; transition: Transition }> = {
  bud: { animate: STILL, transition: { duration: 0.8, ease: EASE } },
  dancing: { animate: SWAY, transition: swayFor(3.4) },
  listening: { animate: SWAY, transition: swayFor(3.4) },
  speaking: { animate: SWAY, transition: swayFor(2.6) },
  thinking: { animate: STILL, transition: { duration: 0.8, ease: EASE } },
};

export const VoiceFlower = memo(function VoiceFlower({ state, spinning, size, level, label, onClick }: VoiceFlowerProps) {
  // A loud voice swells the flower by at most a sixth, on a slow, heavy
  // spring, so it breathes with you instead of jumping at every syllable.
  const grow = useSpring(
    useTransform(level, (l) => 1 + l * 0.16),
    { stiffness: 70, damping: 18, mass: 1.4 },
  );
  const bud = state === "bud";
  const thinking = state === "thinking";

  const turned = useMotionValue(0);
  const spin = useSpring(turned, { stiffness: 40, damping: 14 });
  useEffect(() => {
    if (!spinning) {
      turned.set(Math.ceil(turned.get() / PETAL_TURN) * PETAL_TURN);
      return;
    }
    let raf = 0;
    let last = performance.now();
    const tick = (now: number) => {
      turned.set(turned.get() + ((now - last) / 1000) * SPIN_SPEED);
      last = now;
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [spinning, turned]);

  // Every moving part is an HTML layer wrapped around a static drawing.
  // The watercolour filter is then rasterised once per drawing and the
  // GPU moves the result, instead of the filter re-running every frame of
  // the dance.
  return (
    <button
      type="button"
      aria-label={label}
      onClick={onClick}
      // A click must not pull focus out of the text field, or the next
      // space would press the flower instead of talking.
      onMouseDown={(e) => e.preventDefault()}
      // Space on the flower, focused from the keyboard, is push-to-talk
      // like anywhere else, never a press.
      data-talk
      className="relative flex shrink-0 cursor-pointer items-center justify-center rounded-full bg-transparent p-0 outline-none focus-visible:ring-1 focus-visible:ring-sindoor/70 focus-visible:ring-offset-8 focus-visible:ring-offset-transparent"
      style={{ width: size, height: size }}
    >
      <m.span
        aria-hidden="true"
        className="absolute inset-[18%] rounded-full bg-marigold blur-2xl"
        initial={false}
        animate={{ opacity: state === "speaking" ? 0.32 : 0 }}
        transition={{ duration: 1.2, ease: EASE }}
      />
      {/* The halo fades in and out as a whole, so its breathing never
          starts or stops with a jump. */}
      <m.span
        aria-hidden="true"
        className="absolute inset-[10%]"
        initial={false}
        animate={{ opacity: thinking ? 1 : 0 }}
        transition={{ duration: 0.8, ease: EASE }}
      >
        <m.span
          className="absolute inset-0 rounded-full bg-sindoor blur-2xl"
          initial={false}
          animate={thinking ? { opacity: [0.12, 0.4, 0.12], scale: [0.85, 1.05, 0.85] } : { opacity: 0.12, scale: 0.85 }}
          transition={thinking ? HALO : { duration: 0.8 }}
        />
      </m.span>
      <m.span aria-hidden="true" className="absolute inset-0 will-change-transform" style={{ scale: grow }}>
        <m.span
          className="absolute inset-0 will-change-transform"
          initial={false}
          animate={state === "listening" ? { scale: [1, 1.8], opacity: [0, 0.4, 0] } : { scale: 1, opacity: 0 }}
          transition={state === "listening" ? ECHO : { duration: 0.6 }}
        >
          <PetalArt inner={false} />
        </m.span>
        <m.span
          className="absolute inset-0 will-change-transform"
          initial={false}
          animate={{ scale: bud ? 0.7 : thinking ? 0.84 : 1, rotate: bud ? -26 : 0 }}
          transition={{ duration: 0.9, ease: EASE }}
        >
          <m.span className="absolute inset-0 will-change-transform" style={{ rotate: spin }}>
            <m.span
              className="absolute inset-0 will-change-transform"
              initial={false}
              animate={MOTION[state].animate}
              transition={MOTION[state].transition}
            >
              <PetalArt inner />
            </m.span>
          </m.span>
        </m.span>
      </m.span>
    </button>
  );
});

// The flower drawing itself, never animated from inside. `inner` adds the
// smaller second ring and the core; without it this is the outer ring
// alone, which the listening echo uses.
const PetalArt = memo(function PetalArt({ inner }: { inner: boolean }) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  const petals = RING.map((deg) => <path key={deg} d={PETAL} fill={`url(#petal-${uid})`} transform={`rotate(${deg})`} />);
  return (
    <svg viewBox="-50 -50 100 100" width="100%" height="100%" overflow="visible" className="block">
      <defs>
        <filter id={`wc-${uid}`} x="-40%" y="-40%" width="180%" height="180%">
          <feTurbulence type="fractalNoise" baseFrequency="0.045" numOctaves={3} seed={11} result="n" />
          <feDisplacementMap in="SourceGraphic" in2="n" scale={5} xChannelSelector="R" yChannelSelector="G" result="d" />
          <feGaussianBlur in="d" stdDeviation={0.5} />
        </filter>
        <radialGradient id={`petal-${uid}`} cx="0.5" cy="1" r="1.05">
          <stop offset="0" stopColor={MARIGOLD} stopOpacity={0.95} />
          <stop offset="0.38" stopColor={BLUSH} stopOpacity={0.8} />
          <stop offset="1" stopColor={IVORY} stopOpacity={0.3} />
        </radialGradient>
      </defs>
      <g filter={`url(#wc-${uid})`}>
        {petals}
        {inner && (
          <>
            <g transform="rotate(36) scale(0.58)">{petals}</g>
            <circle r="5" fill={MARIGOLD} />
            <circle r="2" fill={THREAD} />
          </>
        )}
      </g>
    </svg>
  );
});
