import { useId } from "react";
import { motion, useSpring, useTransform, type MotionValue, type TargetAndTransition, type Transition } from "motion/react";

// The flower is voice mode's stand-in for huvu. Closed to a bud while you
// type; open and dancing once voice mode is on. While it listens it swells
// gently with your voice (`level`, 0 to 1). Huvu's own voice goes to the
// garland behind, not to the flower. Where the flower sits on screen is
// the parent's job; this component only draws and animates it in place.
export type FlowerState = "bud" | "dancing" | "listening" | "thinking" | "speaking";

interface VoiceFlowerProps {
  state: FlowerState;
  size: number;
  level: MotionValue<number>;
  label: string;
  onClick?: () => void;
  // Fire TV drives the flower with the remote's select button, so the
  // flower itself must never take focus there.
  focusable?: boolean;
}

const EASE = [0.2, 0.8, 0.2, 1] as const;
const PETAL = "M0,0 C-20,-6 -19,-36 -3,-45 C13,-41 12,-11 0,0Z";
const RING = [0, 72, 144, 216, 288];

const SWAY = { rotate: [0, -6, 6, 0], scale: [1, 0.97, 1.03, 1] };
const STILL = { rotate: 0, scale: 1 };
const swayFor = (seconds: number): Transition => ({ duration: seconds, repeat: Infinity, ease: "easeInOut" });

// How the petals move in each state. Changing state tweens from wherever
// the petals are, so a switch never snaps.
const MOTION: Record<FlowerState, { animate: TargetAndTransition; transition: Transition }> = {
  bud: { animate: STILL, transition: { duration: 0.8, ease: EASE } },
  dancing: { animate: SWAY, transition: swayFor(3.4) },
  listening: { animate: SWAY, transition: swayFor(3.4) },
  speaking: { animate: SWAY, transition: swayFor(2.6) },
  thinking: { animate: { rotate: 360, scale: 1 }, transition: { duration: 9, repeat: Infinity, ease: "linear" } },
};

export function VoiceFlower({ state, size, level, label, onClick, focusable = true }: VoiceFlowerProps) {
  // A loud voice swells the flower by at most a sixth, on a slow, heavy
  // spring, so it breathes with you instead of jumping at every syllable.
  const grow = useSpring(
    useTransform(level, (l) => 1 + l * 0.16),
    { stiffness: 70, damping: 18, mass: 1.4 },
  );
  const bud = state === "bud";

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
      tabIndex={focusable ? 0 : -1}
      className="relative flex shrink-0 cursor-pointer items-center justify-center rounded-full bg-transparent p-0 outline-none focus-visible:ring-1 focus-visible:ring-sindoor/70 focus-visible:ring-offset-8 focus-visible:ring-offset-transparent"
      style={{ width: size, height: size }}
    >
      <motion.span
        aria-hidden="true"
        className="absolute inset-[18%] rounded-full bg-marigold blur-2xl"
        initial={false}
        animate={{ opacity: state === "speaking" ? 0.32 : 0 }}
        transition={{ duration: 1.2, ease: EASE }}
      />
      <motion.span aria-hidden="true" className="absolute inset-0 will-change-transform" style={{ scale: grow }}>
        <motion.span
          className="absolute inset-0 will-change-transform"
          initial={false}
          animate={state === "listening" ? { scale: [1, 1.8], opacity: [0.4, 0] } : { scale: 1, opacity: 0 }}
          transition={state === "listening" ? { duration: 2.4, repeat: Infinity, ease: "easeOut" } : { duration: 0.6 }}
        >
          <PetalArt inner={false} />
        </motion.span>
        <motion.span
          className="absolute inset-0 will-change-transform"
          initial={false}
          animate={{ scale: bud ? 0.7 : 1, rotate: bud ? -26 : 0 }}
          transition={{ duration: 0.9, ease: EASE }}
        >
          <motion.span
            className="absolute inset-0 will-change-transform"
            initial={false}
            animate={MOTION[state].animate}
            transition={MOTION[state].transition}
          >
            <PetalArt inner />
          </motion.span>
        </motion.span>
      </motion.span>
    </button>
  );
}

// The flower drawing itself, never animated from inside. `inner` adds the
// smaller second ring and the core; without it this is the outer ring
// alone, which the listening echo uses.
function PetalArt({ inner }: { inner: boolean }) {
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
          <stop offset="0" stopColor="#e9a23b" stopOpacity={0.95} />
          <stop offset="0.38" stopColor="#f4dccb" stopOpacity={0.8} />
          <stop offset="1" stopColor="#efe7d8" stopOpacity={0.3} />
        </radialGradient>
      </defs>
      <g filter={`url(#wc-${uid})`}>
        {petals}
        {inner && (
          <>
            <g transform="rotate(36) scale(0.58)">{petals}</g>
            <circle r="5" fill="#e9a23b" />
            <circle r="2" fill="#9c3a2e" />
          </>
        )}
      </g>
    </svg>
  );
}
