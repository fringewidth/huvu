import { useRef, type RefObject, type TouchEvent, type WheelEvent } from "react";

// After one scroll gesture changes the view, ignore the rest of it
// (trackpad momentum) for this long.
const GESTURE_LOCK_MS = 700;
const WHEEL_THRESHOLD = 40;
const TOUCH_THRESHOLD = 60;

// Turns wheel and touch movement into one "up" or "down" per gesture.
// The list is the only thing that scrolls, so a gesture counts only once
// the list can't take it.
export function useScrollIntent(listRef: RefObject<HTMLElement | null>, onIntent: (up: boolean) => void) {
  const gesture = useRef({ sum: 0, at: 0, lockUntil: 0 });
  const touchY = useRef<number | null>(null);

  const feed = (dy: number, threshold: number) => {
    const g = gesture.current;
    const now = performance.now();
    const list = listRef.current;
    const listTakes = !!list && (dy < 0 ? list.scrollTop > 0 : list.scrollTop + list.clientHeight < list.scrollHeight - 1);
    if (now < g.lockUntil || listTakes) {
      g.sum = 0;
      return;
    }
    g.sum = (now - g.at > 250 ? 0 : g.sum) + dy;
    g.at = now;
    if (Math.abs(g.sum) < threshold) return;
    const up = g.sum < 0;
    g.sum = 0;
    g.lockUntil = now + GESTURE_LOCK_MS;
    onIntent(up);
  };

  return {
    onWheel: (e: WheelEvent) => feed(e.deltaY, WHEEL_THRESHOLD),
    onTouchStart: (e: TouchEvent) => (touchY.current = e.touches[0].clientY),
    onTouchMove: (e: TouchEvent) => {
      if (touchY.current === null) return;
      const y = e.touches[0].clientY;
      feed(touchY.current - y, TOUCH_THRESHOLD);
      touchY.current = y;
    },
    onTouchEnd: () => (touchY.current = null),
  };
}
