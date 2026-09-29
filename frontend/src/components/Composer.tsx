import { useLayoutEffect, type RefObject } from "react";
import { AnimatePresence, motion } from "motion/react";
import { enterSends, showSendButton, TARGET } from "../config/interaction";
import { land, snapshot, type Snapshot } from "../stage/flight";
import { capsClass } from "../stage/style";

interface ComposerProps {
  ref: RefObject<HTMLDivElement | null>;
  // Empty box the flower sits over in text mode. The flower itself lives
  // in Stage, above everything, so it can glide between here and the
  // middle of the screen without being clipped or faded with the composer.
  slotRef: RefObject<HTMLDivElement | null>;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  shown: boolean;
  draft: string;
  onDraft: (text: string) => void;
  onSubmit: (text: string, from: Snapshot) => void;
  placeholder: string;
  rewinding: boolean;
  onCancelRewind: () => void;
  // A reply is still coming in; the composer offers to stop it.
  running: boolean;
  onStop: () => void;
}

const EASE = [0.2, 0.8, 0.2, 1] as const;

// Your words are always sans serif, the same face as the YOU cue they fly
// up into, so the text can travel without changing its type. The composer
// never leaves the layout: voice mode only fades and lowers it, so nothing
// around it has to reflow.
export function Composer({ ref, slotRef, textareaRef, shown, draft, onDraft, onSubmit, placeholder, rewinding, onCancelRewind, running, onStop }: ComposerProps) {
  const canSend = draft.trim().length > 0;

  // An edit flies the old ask back down into the field.
  useLayoutEffect(() => {
    if (textareaRef.current) land("edit", draft, textareaRef.current);
  }, [draft, textareaRef]);

  const send = () => {
    const el = textareaRef.current;
    if (!el || !canSend) return;
    onSubmit(draft.trim(), snapshot(el));
    // Phone: the keyboard goes down so the answer gets the whole screen.
    if (!enterSends[TARGET]) el.blur();
  };

  return (
    <motion.div
      ref={ref}
      initial={false}
      animate={shown ? { opacity: 1, y: 0 } : { opacity: 0, y: 16 }}
      transition={{ duration: 0.6, ease: EASE }}
      inert={!shown}
      className={`absolute inset-x-0 bottom-0 z-20 ${shown ? "" : "pointer-events-none"}`}
    >
      <div className="mx-auto flex w-full max-w-[52rem] flex-col gap-3 px-6 pb-6 pt-3 sm:px-12 sm:pb-10">
        <div className="flex min-h-5 items-center gap-3">
          <span className="h-px w-5 bg-thread" />
          <label htmlFor="huvu-ask" className={`${capsClass} text-sindoor`}>
            {rewinding ? "rewound to here" : "to huvu"}
          </label>
          <AnimatePresence>
            {rewinding && (
              <motion.button
                type="button"
                onClick={onCancelRewind}
                initial={{ opacity: 0, x: -6 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -6 }}
                transition={{ duration: 0.5, ease: EASE }}
                className={`${capsClass} -my-3 min-h-11 px-2 text-ivory/60 transition-colors duration-500 hover:text-ivory`}
              >
                cancel
              </motion.button>
            )}
          </AnimatePresence>
          <AnimatePresence>
            {running && (
              <motion.button
                type="button"
                onClick={onStop}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.4, ease: EASE }}
                className={`${capsClass} -my-3 ml-auto flex min-h-11 items-center gap-2 px-2 text-ivory/60 transition-colors duration-500 hover:text-ivory`}
              >
                <span className="size-1.5 bg-sindoor" />
                stop
              </motion.button>
            )}
          </AnimatePresence>
        </div>
        <div className="flex items-end gap-3 sm:gap-5">
          <textarea
            id="huvu-ask"
            ref={textareaRef}
            rows={1}
            value={draft}
            placeholder={placeholder}
            enterKeyHint={enterSends[TARGET] ? "send" : "enter"}
            onChange={(e) => onDraft(e.target.value)}
            onKeyDown={(e) => {
              if (!enterSends[TARGET] || e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
              e.preventDefault();
              send();
            }}
            className="halo max-h-[36dvh] min-w-0 flex-1 resize-none border-0 border-b border-sindoor/40 bg-transparent pb-2 font-display text-[20px] font-medium leading-[1.4] text-ivory caret-sindoor outline-none transition-colors duration-500 [field-sizing:content] placeholder:text-ivory/40 focus:border-sindoor/90 sm:text-[24px]"
          />
          <AnimatePresence>
            {showSendButton[TARGET] && canSend && (
              <motion.button
                type="button"
                onClick={send}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.4, ease: EASE }}
                className={`${capsClass} mb-1 flex min-h-11 shrink-0 items-center px-3 text-ivory`}
              >
                send
              </motion.button>
            )}
          </AnimatePresence>
          <div ref={slotRef} aria-hidden="true" className="size-14 shrink-0" />
        </div>
      </div>
    </motion.div>
  );
}
