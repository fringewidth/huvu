import { memo, useLayoutEffect, useRef, useState, type Ref } from "react";
import * as m from "motion/react-m";
import { AnimatePresence, type Variants } from "motion/react";
import { ui } from "../config/interaction";
import { land, snapshot, type Snapshot } from "../stage/flight";
import { chipsFor, kindOf, partsInSlot, type Slot } from "../stage/parts";
import { capsClass, EASE } from "../stage/style";
import type { ImagePart, RichPart, ToolPart, Turn } from "../chat/thread";

// One turn, in one of two sizes. As the hero it is the whole stage: the
// answer set large, images and panels open. As a history row it sits at
// the same size as every other row, truncated, with chips standing in for
// whatever it holds.
//
// Everything that moves here moves by transform and opacity only. The row
// slides to its new place with a layout animation; the answer text, whose
// size changes, crossfades: the old copy scales toward the new size and
// fades out while the new copy scales in from the old size.
export type TurnVariant = "hero" | "row";

export interface TurnActions {
  open(id: string): void;
  edit(turn: Turn, from: Snapshot): void;
  choose(text: string): void;
  retry(turn: Turn): void;
  // Fire TV moves focus between rows and their rewind buttons by hand.
  registerFocus(id: string, which: "open" | "edit", el: HTMLButtonElement | null): void;
}

interface TurnViewProps {
  ref?: Ref<HTMLLIElement>;
  turn: Turn;
  variant: TurnVariant;
  wide: boolean;
  // Off on the phone's voice stage (see voiceAnswerText): the answer and
  // the thinking thread fade out, and the ask, the parts and how the
  // answer ended stay.
  answerShown: boolean;
  actions: TurnActions;
}

// Short answers are set huge; longer ones step down as they stream in.
const HERO_TIERS = [
  { under: 90, narrow: 34, wide: 46, lh: 1.1 },
  { under: 240, narrow: 26, wide: 34, lh: 1.18 },
  { under: Infinity, narrow: 20, wide: 24, lh: 1.35 },
];
const ROW = { narrow: 17, wide: 19, lh: 1.35 };

function answerSize(answer: string, variant: TurnVariant, wide: boolean): { key: string; fontSize: number; lineHeight: number } {
  const tier = variant === "row" ? -1 : HERO_TIERS.findIndex((t) => answer.length < t.under);
  const spec = tier < 0 ? ROW : HERO_TIERS[tier];
  return { key: `${variant}${tier}`, fontSize: wide ? spec.wide : spec.narrow, lineHeight: spec.lh };
}

// `custom` is old size / new size. The entering copy starts at that scale
// (so it looks old-sized) and settles to 1; the leaving copy, old-sized,
// shrinks or grows by the inverse toward the new size.
const crossfade: Variants = {
  enter: (ratio: number) => ({ opacity: 0, scale: ratio }),
  shown: { opacity: 1, scale: 1, transition: { duration: 0.7, ease: EASE } },
  leave: (ratio: number) => ({ opacity: 0, scale: 1 / ratio, transition: { duration: 0.5, ease: EASE } }),
};

const fade = {
  initial: { opacity: 0 },
  animate: { opacity: 1, transition: { duration: 0.5, delay: 0.15, ease: EASE } },
  exit: { opacity: 0, transition: { duration: 0.25, ease: EASE } },
};

export const TurnView = memo(function TurnView({ ref, turn, variant, wide, answerShown, actions }: TurnViewProps) {
  const cueRef = useRef<HTMLSpanElement>(null);
  const hero = variant === "hero";
  const chips = chipsFor(turn.parts);
  const size = answerSize(turn.answer, variant, wide);

  // The size the answer had before its latest size change, kept until the
  // next one, so the new copy knows what it is crossfading from.
  const [shape, setShape] = useState({ key: size.key, fontSize: size.fontSize, ratio: 1 });
  if (shape.key !== size.key) setShape({ key: size.key, fontSize: size.fontSize, ratio: shape.fontSize / size.fontSize });

  // A freshly sent ask lands here from the composer or the transcript.
  useLayoutEffect(() => {
    if (cueRef.current) land("ask", turn.ask, cueRef.current);
  }, [turn.ask]);

  const fold = (slot: Slot) => {
    const here = hero ? partsInSlot(turn.parts, slot) : [];
    return (
      <AnimatePresence initial={false} mode="popLayout">
        {here.length > 0 && (
          <m.div key={slot} {...fade}>
            <HeroParts here={here} onChoose={actions.choose} />
          </m.div>
        )}
      </AnimatePresence>
    );
  };

  return (
    <m.li
      ref={ref}
      layout="position"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0, transition: { duration: 0.35, ease: EASE } }}
      transition={{ layout: { duration: 0.8, ease: EASE }, default: { duration: 0.6, ease: EASE } }}
      className="relative flex flex-col gap-3 sm:gap-4"
    >
      {!hero && (
        <button
          ref={(el) => actions.registerFocus(turn.id, "open", el)}
          type="button"
          aria-label={`open: ${turn.ask}`}
          onClick={() => actions.open(turn.id)}
          className="absolute -inset-x-3 -inset-y-2 z-0 cursor-pointer rounded-sm outline-none transition-colors duration-500 hover:bg-ivory/[0.03] focus-visible:bg-ivory/[0.05] focus-visible:ring-1 focus-visible:ring-sindoor/50 in-data-[target=firetv]:focus-visible:ring-2"
        />
      )}
      <div className={`relative z-10 flex items-baseline gap-3 ${hero ? "" : "pointer-events-none"}`}>
        <span className={`${capsClass} shrink-0 text-sindoor`}>you</span>
        <span
          ref={cueRef}
          className={`halo min-w-0 font-display font-medium leading-[1.4] text-ivory/80 ${
            hero ? "whitespace-pre-wrap text-[14px] sm:text-[15px]" : "truncate text-[13px] sm:text-[14px]"
          }`}
        >
          {turn.ask}
        </span>
        <button
          ref={(el) => actions.registerFocus(turn.id, "edit", el)}
          type="button"
          onClick={() => cueRef.current && actions.edit(turn, snapshot(cueRef.current))}
          className={`${capsClass} pointer-events-auto -my-3 ml-auto min-h-11 shrink-0 px-2 text-ivory/45 outline-none transition-colors duration-500 hover:text-ivory focus-visible:text-sindoor focus-visible:ring-1 focus-visible:ring-sindoor/60 in-data-[target=firetv]:focus-visible:ring-2`}
        >
          {ui.editLabel}
        </button>
      </div>

      {fold("note")}

      <div className={`relative z-0 ${hero ? "" : "pointer-events-none"}`}>
        {answerShown && turn.running && !turn.answer && <span className="thinking-thread" aria-label="huvu is thinking" />}
        {turn.answer && (
          <AnimatePresence initial={false} mode="popLayout" custom={shape.ratio}>
            {answerShown && (
              <m.p
                key={size.key}
                custom={shape.ratio}
                variants={crossfade}
                initial="enter"
                animate="shown"
                exit="leave"
                style={{ fontSize: size.fontSize, lineHeight: size.lineHeight, transformOrigin: "0 0" }}
                className={`halo m-0 whitespace-pre-wrap font-bodoni italic [text-wrap:pretty] ${
                  hero ? "text-ivory" : "fade-bottom max-h-[2.7em] overflow-hidden text-ivory/75"
                }`}
              >
                {/* Words fade in one by one only on the answer's first
                    appearance, not again on every size change. */}
                <AnswerText text={turn.answer} streaming={turn.running && shape.ratio === 1} />
              </m.p>
            )}
          </AnimatePresence>
        )}
        {turn.ending && !turn.running && (
          <div className="mt-3 flex items-center gap-4">
            <span className={`${capsClass} text-sindoor`}>{turn.ending === "failed" ? "huvu couldn't answer" : "stopped"}</span>
            {turn.ending === "failed" && hero && (
              <button
                type="button"
                onClick={() => actions.retry(turn)}
                className={`${capsClass} -my-3 min-h-11 px-2 text-ivory/70 transition-colors duration-500 hover:text-ivory`}
              >
                try again
              </button>
            )}
          </div>
        )}
      </div>

      {fold("panel")}
      {fold("choices")}

      <AnimatePresence initial={false} mode="popLayout">
        {!hero && chips.length > 0 && (
          <m.div key="chips" {...fade} className="pointer-events-none relative z-10 flex flex-wrap gap-2">
            {chips.map((chip) => (
              <span key={chip} className={`${capsClass} rounded-full border border-ivory/20 px-3 py-1.5 text-ivory/60`}>
                {chip}
              </span>
            ))}
          </m.div>
        )}
      </AnimatePresence>
    </m.li>
  );
});

function HeroParts({ here, onChoose }: { here: RichPart[]; onChoose: (text: string) => void }) {
  const images = here.filter((p): p is ImagePart => p.kind === "image");
  const tools = here.filter((p): p is ToolPart => p.kind === "tool");
  return (
    <div className="flex flex-col gap-4">
      {images.length > 0 && (
        <div className={`grid max-w-[36rem] gap-3 ${images.length === 1 ? "grid-cols-1" : "grid-cols-2"}`}>
          {images.map((img, i) => (
            <img key={i} src={img.src} alt="" className="aspect-[4/3] w-full border border-ivory/15 object-cover" />
          ))}
        </div>
      )}
      {tools.map((p) => {
        const { Hero } = kindOf(p.toolName);
        return <Hero key={p.id} part={p} onChoose={onChoose} />;
      })}
    </div>
  );
}

// While huvu is still answering, new words fade in one after another,
// even when a whole chunk arrives at once. A word keeps its fade once it
// has one: the animation runs once on mount, and dropping the class when
// streaming ends would cut short the words still fading in.
function AnswerText({ text, streaming }: { text: string; streaming: boolean }) {
  const pieces = text.split(/(\s+)/);
  const [timing, setTiming] = useState<{ count: number; delays: (number | undefined)[] }>({ count: 0, delays: [] });

  if (pieces.length !== timing.count) {
    const delays = timing.delays.slice(0, pieces.length);
    for (let i = timing.count; streaming && i < pieces.length; i++) {
      if (pieces[i].trim()) delays[i] = Math.min((i - timing.count) * 18, 1400);
    }
    setTiming({ count: pieces.length, delays });
  }

  return pieces.map((piece, i) => {
    const delay = timing.delays[i];
    return (
      <span key={i} className={delay === undefined ? undefined : "word-in"} style={delay === undefined ? undefined : { animationDelay: `${delay}ms` }}>
        {piece}
      </span>
    );
  });
}
