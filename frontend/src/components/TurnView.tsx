import { memo, useLayoutEffect, useRef, useState, type Ref } from "react";
import * as m from "motion/react-m";
import { AnimatePresence, type Variants } from "motion/react";
import { openedWake } from "../account/opens";
import { ui } from "../config/interaction";
import { land, snapshot, type Snapshot } from "../stage/flight";
import { chipsFor, interleave, kindOf, partsInSlot, type Slot } from "../stage/parts";
import { capsClass, EASE } from "../stage/style";
import type { ImagePart, Notice, RichPart, ToolPart, Turn } from "../chat/thread";
import type { MenuSection } from "./Menu";

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
  // Opens the menu at the section a notice is about.
  show(section: MenuSection): void;
  // Puts the wake a notice came from on the stage.
  openWake(id: string): void;
  // Fire TV moves focus between history rows by hand.
  registerFocus(id: string, el: HTMLButtonElement | null): void;
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
  // Offers edit and retry. A wake has neither: Hermes keeps everything its
  // run did.
  rewindable: boolean;
  actions: TurnActions;
}

// Short answers are set huge; longer ones step down as they stream in.
const HERO_TIERS = [
  { under: 90, narrow: 34, wide: 46, lh: 1.1 },
  { under: 240, narrow: 24, wide: 30, lh: 1.25 },
  { under: Infinity, narrow: 18, wide: 20, lh: 1.55 },
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
// The wake a notice came from, which it opens.
function wakeOf(notice: Notice | null): string | null {
  return openedWake(notice?.opens ?? null);
}

// A notice's cue: who sent it, and "urgent" first when it is.
function noticeCue(notice: Notice): string {
  const urgent = notice.urgency === "high";
  return urgent && !notice.from ? "urgent" : `${urgent ? "urgent, " : ""}from ${notice.from ?? "huvu"}`;
}

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

export const TurnView = memo(function TurnView({ ref, turn, variant, wide, answerShown, rewindable, actions }: TurnViewProps) {
  const cueRef = useRef<HTMLSpanElement>(null);
  const hero = variant === "hero";
  const chips = chipsFor(turn.parts);
  const size = answerSize(turn.answer, variant, wide);
  const pieces = interleave(turn.answer, hero ? partsInSlot(turn.parts, "note") : []);

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
          ref={(el) => actions.registerFocus(turn.id, el)}
          type="button"
          aria-label={`open: ${turn.ask || turn.answer}`}
          onClick={() => actions.open(turn.id)}
          className="absolute -inset-x-3 -inset-y-2 z-0 cursor-pointer rounded-sm outline-none transition-colors duration-500 hover:bg-ivory/[0.03] focus-visible:bg-ivory/[0.05] focus-visible:ring-1 focus-visible:ring-sindoor/50 in-data-[target=firetv]:focus-visible:ring-2"
        />
      )}
      {/* A watched feed asked nothing, so it has no YOU cue: just its name
          and the feed. */}
      {turn.ask && (
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
          {ui.hasInput && rewindable && (
            <button
              type="button"
              onClick={() => cueRef.current && actions.edit(turn, snapshot(cueRef.current))}
              className={`${capsClass} pointer-events-auto -my-3 ml-auto min-h-11 shrink-0 px-2 text-ivory/45 outline-none transition-colors duration-500 hover:text-ivory focus-visible:text-sindoor focus-visible:ring-1 focus-visible:ring-sindoor/60`}
            >
              edit
            </button>
          )}
        </div>
      )}

      {/* A notice asked nothing either. Its cue says who sent it unasked,
          huvu or someone in the family, and how urgent it is. */}
      {turn.notice && (
        <div className={`relative z-10 flex items-baseline gap-3 ${hero ? "" : "pointer-events-none"}`}>
          <span className={`${capsClass} shrink-0 ${turn.notice.urgency === "high" ? "text-sindoor" : "text-ivory/55"}`}>
            {noticeCue(turn.notice)}
          </span>
          {hero && turn.notice.opens === "triggers" && (
            <button
              type="button"
              onClick={() => actions.show("triggers")}
              className={`${capsClass} pointer-events-auto -my-3 ml-auto min-h-11 shrink-0 px-2 text-ivory/70 outline-none transition-colors duration-500 hover:text-ivory focus-visible:text-sindoor focus-visible:ring-1 focus-visible:ring-sindoor/60`}
            >
              see triggers
            </button>
          )}
          {hero && wakeOf(turn.notice) && (
            <button
              type="button"
              onClick={() => actions.openWake(wakeOf(turn.notice) ?? "")}
              className={`${capsClass} pointer-events-auto -my-3 ml-auto min-h-11 shrink-0 px-2 text-ivory/70 outline-none transition-colors duration-500 hover:text-ivory focus-visible:text-sindoor focus-visible:ring-1 focus-visible:ring-sindoor/60`}
            >
              open
            </button>
          )}
        </div>
      )}

      <div className={`relative z-0 ${hero ? "" : "pointer-events-none"}`}>
        {/* The answer with huvu's notes where it ran each tool. A row
            shows the answer alone. With the answer hidden (the phone's
            voice stage), the notes stay where they were. */}
        <div className="flex flex-col gap-3 sm:gap-4">
          {pieces.map((piece, i) =>
            "notes" in piece ? (
              <m.div key={i} {...fade}>
                <HeroParts here={piece.notes} onChoose={actions.choose} />
              </m.div>
            ) : (
              <AnimatePresence key={i} initial={false} mode="popLayout" custom={shape.ratio}>
                {answerShown && (
                  <m.p
                    key={size.key}
                    custom={shape.ratio}
                    variants={crossfade}
                    initial="enter"
                    animate="shown"
                    exit="leave"
                    style={{ fontSize: size.fontSize, lineHeight: size.lineHeight, transformOrigin: "0 0" }}
                    className={`halo m-0 whitespace-pre-wrap font-reading [text-wrap:pretty] ${
                      hero ? "text-ivory" : "fade-bottom max-h-[2.7em] overflow-hidden text-ivory/75"
                    }`}
                  >
                    {/* Words fade in one by one only on the answer's first
                        appearance, not again on every size change. */}
                    <AnswerText text={piece.text} streaming={turn.running && shape.ratio === 1} />
                  </m.p>
                )}
              </AnimatePresence>
            ),
          )}
        </div>
        {answerShown && turn.running && !turn.answer && <span className="thinking-thread" aria-label="huvu is thinking" />}
        {/* huvu's reasoning summary while it works: faint, the newest lines
            of the current burst, gone once the turn ends. */}
        <AnimatePresence>
          {hero && answerShown && turn.running && turn.thinking && (
            <m.div
              key="thinking"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.6, ease: EASE }}
              aria-live="polite"
              className={`${turn.thinking.length > 180 ? "thinking-text" : ""} mt-4 flex max-h-[4.5em] flex-col justify-end overflow-hidden font-reading text-[15px] leading-[1.5] text-ivory/45 sm:text-[16px]`}
            >
              <p className="halo m-0 whitespace-pre-wrap">{turn.thinking.replace(/\*\*/g, "")}</p>
            </m.div>
          )}
        </AnimatePresence>
        {turn.ending && !turn.running && (
          <div className="mt-3 flex items-center gap-4">
            <span className={`${capsClass} text-sindoor`}>{turn.ending === "failed" ? "huvu couldn't answer" : "stopped"}</span>
            {turn.ending === "failed" && hero && ui.hasInput && rewindable && (
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
            <figure key={i} className="flex min-w-0 flex-col gap-2">
              {/* One image shows whole, since a screenshot or a chart cropped loses its point. Several are tiles. */}
              <img
                src={img.src}
                alt={img.caption ?? ""}
                className={`w-full border border-ivory/15 ${images.length === 1 ? "max-h-[60vh] object-contain" : "aspect-[4/3] object-cover"}`}
              />
              {img.caption && <figcaption className="font-display text-sm text-ivory/50">{img.caption}</figcaption>}
            </figure>
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
