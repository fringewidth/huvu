import { useLayoutEffect, useRef, useState } from "react";

type Handlers = Record<string, (...args: never[]) => unknown>;

// Wraps this render's handlers in functions that never change, so a
// memoised child keeps equal props across renders and still calls the
// latest handler. The set of keys is fixed at the first render.
export function useStable<T extends Handlers>(handlers: T): T {
  const latest = useRef(handlers);
  useLayoutEffect(() => {
    latest.current = handlers;
  });
  const [stable] = useState(() => {
    const out: Handlers = {};
    for (const key of Object.keys(handlers)) out[key] = (...args) => latest.current[key](...args);
    return out as T;
  });
  return stable;
}
