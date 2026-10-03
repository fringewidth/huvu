import { memo, useEffect, type RefObject } from "react";
import * as m from "motion/react-m";
import { AnimatePresence } from "motion/react";
import { refreshFeeds, useFeeds, type Feed } from "../chat/feeds";
import { TV } from "../config/interaction";
import { capsClass, EASE, NUMERALS } from "../stage/style";

interface FeedsProps {
  open: boolean;
  onToggle: () => void;
  onPick: (feed: Feed) => void;
  // Fire TV moves focus between the rows by hand (see Stage).
  listRef: RefObject<HTMLDivElement | null>;
}

// The live feeds huvu could show, opposite the wordmark. The list is text
// only, with no previews: a preview would be an open feed. Picking a feed
// only watches it: it becomes a turn of its own, the feed's name over the
// feed, and huvu is never asked. With no feeds listed, nothing renders at
// all.
export const Feeds = memo(function Feeds({ open, onToggle, onPick, listRef }: FeedsProps) {
  const feeds = useFeeds();
  const live = feeds.filter((f) => f.online).length;

  useEffect(() => {
    if (open) void refreshFeeds();
  }, [open]);

  // With no feeds there is no list to draw, so an open list closes again.
  // Otherwise the Fire TV D-pad would stay with a list nobody can see.
  useEffect(() => {
    if (open && feeds.length === 0) onToggle();
  }, [open, feeds.length, onToggle]);

  // Fire TV: the list opens with the first feed that can be watched in
  // focus.
  useEffect(() => {
    if (TV && open) listRef.current?.querySelector<HTMLButtonElement>("button:enabled")?.focus({ preventScroll: true });
  }, [open, listRef]);

  if (feeds.length === 0) return null;

  return (
    <>
      {/* A click anywhere outside the list closes it. */}
      {open && <div aria-hidden="true" onClick={onToggle} className="absolute inset-0 z-20" />}
      <button
        type="button"
        aria-expanded={open}
        aria-label={`live feeds, ${live} online`}
        onClick={onToggle}
        className={`${capsClass} halo absolute right-4 top-3.5 z-20 flex min-h-11 items-center gap-2.5 px-2 outline-none transition-colors duration-500 focus-visible:ring-1 focus-visible:ring-sindoor/60 sm:right-10 sm:top-[26px] ${
          open ? "text-sindoor" : "text-ivory/70 hover:text-ivory"
        }`}
      >
        <span className={`size-1.5 shrink-0 rounded-full ${live ? "bg-sindoor" : "bg-ivory/30"}`} />
        live / {live}
      </button>
      <AnimatePresence>
        {open && (
          <m.div
            ref={listRef}
            initial={{ opacity: 0, y: -6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.4, ease: EASE }}
            className="absolute inset-x-4 top-16 z-30 flex flex-col border border-ivory/15 bg-paper/95 px-5 pb-2.5 pt-4 sm:inset-x-auto sm:right-12 sm:top-20 sm:w-96"
          >
            <span className={`${capsClass} pb-1.5 text-ivory/45`}>feeds</span>
            {feeds.map((feed, i) => (
              <button
                key={feed.id}
                type="button"
                disabled={!feed.online}
                onClick={() => onPick(feed)}
                className="flex min-h-12 items-baseline gap-3 pt-3 text-left font-bodoni text-[21px] text-ivory outline-none transition-colors duration-500 enabled:hover:text-sindoor focus-visible:text-sindoor focus-visible:ring-1 focus-visible:ring-sindoor/50 disabled:cursor-default disabled:text-ivory/35 in-data-[target=firetv]:focus-visible:ring-2"
              >
                <span className={`${capsClass} ${feed.online ? "text-sindoor" : "text-ivory/30"}`}>{NUMERALS[i] ?? `${i + 1}.`}</span>
                <span className="whitespace-nowrap">{feed.label}</span>
                <span className={`${capsClass} ml-auto pl-4 text-ivory/40`}>{feed.online ? feed.kind : "offline"}</span>
              </button>
            ))}
            {TV && <span className={`${capsClass} pb-1.5 pt-3 text-ivory/50`}>select to watch / back to close</span>}
          </m.div>
        )}
      </AnimatePresence>
    </>
  );
});
