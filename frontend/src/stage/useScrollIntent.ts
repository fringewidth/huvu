import { useEffect, useRef, type RefObject, type TouchEvent, type WheelEvent } from "react";

// A gesture is one touch, or a run of wheel events with no gap longer
// than this (trackpad momentum included).
const GESTURE_GAP_MS = 250;
// The list must have been still this long before a gesture past its edge
// counts. Covers the fling inertia a phone keeps scrolling with after the
// finger lifts, which no touch event reports.
const EDGE_REST_MS = 400;
const WHEEL_THRESHOLD = 40;
const TOUCH_THRESHOLD = 60;

// Turns wheel and touch movement into one "up" or "down" per gesture.
// The list is the only thing that scrolls, so a gesture counts only once
// the list can't take it, and only if it didn't scroll the list itself:
// reading up to the top of a long answer and carrying on never flips the
// view. A fresh gesture from the edge does.
export function useScrollIntent(listRef: RefObject<HTMLElement | null>, onIntent: (up: boolean) => void) {
  // `spent`: this gesture scrolled the list, or already fired.
  const gesture = useRef({ sum: 0, spent: false, wheelAt: 0 });
  const listMovedAt = useRef(0);
  // Where the list was at its last scroll event. Chrome's compositor
  // scrolls the list before a (passive) wheel or touch event reaches us,
  // and the scroll event only follows a frame later, so a list found
  // somewhere else has just taken the event.
  const listTop = useRef(0);
  const touchY = useRef<number | null>(null);

  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    listTop.current = list.scrollTop;
    const moved = () => {
      listMovedAt.current = performance.now();
      listTop.current = list.scrollTop;
      gesture.current.spent = true;
    };
    list.addEventListener("scroll", moved, { passive: true });
    return () => list.removeEventListener("scroll", moved);
  }, [listRef]);

  const begin = () => {
    gesture.current.sum = 0;
    gesture.current.spent = false;
  };

  const feed = (dy: number, threshold: number) => {
    const g = gesture.current;
    const list = listRef.current;
    const now = performance.now();
    const listTakes = !!list && (dy < 0 ? list.scrollTop > 0 : list.scrollTop + list.clientHeight < list.scrollHeight - 1);
    const listMoved = !!list && list.scrollTop !== listTop.current;
    if (list) listTop.current = list.scrollTop;
    if (listTakes || listMoved) g.spent = true;
    if (g.spent || now - listMovedAt.current < EDGE_REST_MS) {
      g.sum = 0;
      return;
    }
    g.sum += dy;
    if (Math.abs(g.sum) < threshold) return;
    g.spent = true;
    onIntent(g.sum < 0);
  };

  return {
    onWheel: (e: WheelEvent) => {
      const now = performance.now();
      if (now - gesture.current.wheelAt > GESTURE_GAP_MS) begin();
      gesture.current.wheelAt = now;
      feed(e.deltaY, WHEEL_THRESHOLD);
    },
    onTouchStart: (e: TouchEvent) => {
      begin();
      touchY.current = e.touches[0].clientY;
    },
    onTouchMove: (e: TouchEvent) => {
      if (touchY.current === null) return;
      const y = e.touches[0].clientY;
      feed(touchY.current - y, TOUCH_THRESHOLD);
      touchY.current = y;
    },
    onTouchEnd: () => (touchY.current = null),
  };
}
