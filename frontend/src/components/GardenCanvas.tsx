import { useEffect, useRef } from "react";
import { startGarden } from "../canvas/host";

export function GardenCanvas() {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => startGarden(ref.current!), []);
  // z-0, not a negative z-index: #root is a flex container, and a negative
  // layer there ends up behind body's own background.
  return <div ref={ref} className="fixed inset-0 z-0 [&>canvas]:block" />;
}
