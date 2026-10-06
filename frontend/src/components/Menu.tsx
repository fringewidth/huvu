import { lazy, memo, Suspense } from "react";
import * as m from "motion/react-m";
import { AnimatePresence } from "motion/react";
import { useHousehold } from "../account/household";
import { capsClass, EASE } from "../stage/style";

// The panel loads the first time the menu opens.
const MenuPanel = lazy(() => import("./MenuPanel").then((mod) => ({ default: mod.MenuPanel })));

// Where the menu opens: at its top, or scrolled to one section (a notice
// about a paused trigger opens the triggers).
export type MenuSection = "top" | "triggers";

interface MenuProps {
  open: MenuSection | null;
  onToggle: () => void;
  onClose: () => void;
}

// The wordmark opens the menu (MenuPanel.tsx). A small mark by the wordmark says when a
// trigger waits for a tap: one the wake limit paused comes back only that
// way. It mirrors the live feeds list opposite it.
export const Menu = memo(function Menu({ open, onToggle, onClose }: MenuProps) {
  const { triggers } = useHousehold();
  const needsTap = triggers.some((t) => t.status === "paused" && t.paused_by === "limit");
  return (
    <>
      {open && <div aria-hidden="true" onClick={onClose} className="absolute inset-0 z-20" />}
      <button
        type="button"
        aria-expanded={open !== null}
        aria-label={needsTap ? "menu, a trigger needs you" : "menu"}
        onClick={onToggle}
        className={`${capsClass} halo absolute left-4 top-3.5 z-20 flex min-h-11 items-center gap-2.5 px-2 text-[11px] tracking-[0.42em] outline-none transition-colors duration-500 focus-visible:ring-1 focus-visible:ring-sindoor/60 sm:left-10 sm:top-[26px] ${
          open ? "text-sindoor" : "text-ivory/75 hover:text-ivory"
        }`}
      >
        huvu
        {needsTap && <span className="size-1.5 shrink-0 rounded-full bg-sindoor" />}
      </button>
      <AnimatePresence>
        {open && (
          <m.div
            initial={{ opacity: 0, y: -6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.4, ease: EASE }}
            className="absolute inset-x-4 top-16 z-30 max-h-[calc(100dvh-5.5rem)] overflow-y-auto border border-ivory/15 bg-paper/95 px-5 pb-5 pt-4 [scrollbar-width:thin] sm:inset-x-auto sm:left-12 sm:top-20 sm:max-h-[calc(100dvh-7rem)] sm:w-[28rem]"
          >
            <Suspense fallback={<span className={`${capsClass} text-ivory/45`}>a moment</span>}>
              <MenuPanel section={open} />
            </Suspense>
          </m.div>
        )}
      </AnimatePresence>
    </>
  );
});
