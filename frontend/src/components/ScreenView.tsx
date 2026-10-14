import { useEffect, useRef, useState } from "react";
import * as m from "motion/react-m";
import { BACKEND, postJson } from "../net/http";
import { capsClass, EASE } from "../stage/style";

type ScreenState = "connecting" | "on" | "off";

// huvu's browser, live: its profile's screen, which Hermes's browser draws
// on. The backend relays it from Hermes's dashboard (backend/src/huvu/
// screen.py), and noVNC plays it here, to watch only. The screen runs
// only while huvu uses the browser, so a closed one says so. noVNC loads
// the first time it opens.
export function ScreenView({ onClose }: { onClose: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<ScreenState>("connecting");

  useEffect(() => {
    let gone = false;
    let close: (() => void) | null = null;
    void (async () => {
      const [{ ticket }, { default: RFB }] = await Promise.all([postJson<{ ticket: string }>("/api/screen/ticket"), import("@novnc/novnc")]);
      if (gone || !box.current) return;
      const url = `${BACKEND.replace(/^http/, "ws")}/api/screen?ticket=${encodeURIComponent(ticket)}`;
      const rfb = new RFB(box.current, url, { shared: true });
      rfb.viewOnly = true;
      rfb.scaleViewport = true;
      rfb.background = "transparent";
      rfb.addEventListener("connect", () => setState("on"));
      rfb.addEventListener("disconnect", () => setState("off"));
      close = () => rfb.disconnect();
    })().catch((err: unknown) => {
      console.error("screen: could not open it", err);
      if (!gone) setState("off");
    });
    return () => {
      gone = true;
      close?.();
    };
  }, []);

  const label = state === "on" ? "huvu's browser, live" : state === "connecting" ? "a moment" : "huvu's browser isn't open right now";
  return (
    <m.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.4, ease: EASE }}
      className="absolute inset-0 z-40 flex flex-col gap-3 bg-paper/95 px-4 pb-4 pt-4 sm:px-10 sm:pt-6"
    >
      <div className="flex items-baseline gap-4">
        <span className={`${capsClass} ${state === "on" ? "text-sindoor" : "text-ivory/55"}`}>{label}</span>
        <button
          type="button"
          onClick={onClose}
          className={`${capsClass} -my-3 ml-auto min-h-11 shrink-0 px-2 text-ivory/70 outline-none transition-colors duration-500 hover:text-ivory focus-visible:ring-1 focus-visible:ring-sindoor/60`}
        >
          close
        </button>
      </div>
      <div ref={box} className="min-h-0 flex-1 overflow-hidden" />
    </m.div>
  );
}
