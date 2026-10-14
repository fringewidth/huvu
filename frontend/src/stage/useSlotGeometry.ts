import { useLayoutEffect, useState, type RefObject } from "react";

export interface Geometry {
  w: number;
  h: number;
  // Center of the flower's slot in the composer.
  slotX: number;
  slotY: number;
  composerH: number;
}

// Where the flower's composer slot is, and how tall the composer is.
// Offsets, not bounding rects: they ignore the composer's own fade and
// lower transform in voice mode. `composerShown` says whether the composer
// is mounted: it comes and goes (an ended wake has none), and a composer
// that mounts later has to be measured and watched too.
export function useSlotGeometry(
  rootRef: RefObject<HTMLElement | null>,
  slotRef: RefObject<HTMLElement | null>,
  composerRef: RefObject<HTMLElement | null>,
  composerShown: boolean,
): Geometry | null {
  const [geo, setGeo] = useState<Geometry | null>(null);
  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const measure = () => {
      const slot = slotRef.current;
      let x = root.clientWidth / 2;
      let y = root.clientHeight;
      if (slot) {
        x = slot.offsetWidth / 2;
        y = slot.offsetHeight / 2;
        for (let el: HTMLElement | null = slot; el && el !== root; el = el.offsetParent as HTMLElement | null) {
          x += el.offsetLeft;
          y += el.offsetTop;
        }
      }
      setGeo({
        w: root.clientWidth,
        h: root.clientHeight,
        slotX: x,
        slotY: y,
        composerH: composerRef.current?.offsetHeight ?? 0,
      });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(root);
    if (composerRef.current) ro.observe(composerRef.current);
    return () => ro.disconnect();
  }, [rootRef, slotRef, composerRef, composerShown]);
  return geo;
}
