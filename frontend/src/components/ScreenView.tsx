import { useEffect, useRef, useState, type FormEvent } from "react";
import * as m from "motion/react-m";
import type RFB from "@novnc/novnc";
import { BACKEND, postJson } from "../net/http";
import { capsClass, EASE } from "../stage/style";

type ScreenState = "connecting" | "on" | "off";

// X11 keysyms: Latin-1 maps straight, the rest of Unicode sits at
// 0x01000000 plus its code point.
const BACKSPACE = 0xff08;
const RETURN = 0xff0d;
const keysymOf = (ch: string) => {
  const code = ch.codePointAt(0) ?? 0;
  return code < 0x100 ? code : 0x01000000 + code;
};
// A touch screen has no keys to send, so typing goes through a hidden field
// that opens the screen's own keyboard.
const touch = typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches;

const button = `${capsClass} -my-3 min-h-11 shrink-0 px-2 text-ivory/70 outline-none transition-colors duration-500 hover:text-ivory focus-visible:ring-1 focus-visible:ring-sindoor/60`;

// huvu's browser, live: its profile's screen, which Hermes's browser draws
// on. The backend relays it from Hermes's dashboard (backend/src/huvu/
// screen.py), and noVNC plays it here. The person watches it, and can take
// it over to click and type in it and hear it, and hand it back; while they
// hold it, huvu's browser tools wait. Closing the view hands it back. The
// screen runs only while huvu uses the browser, so a closed one says so.
// noVNC loads the first time it opens.
export function ScreenView({ onClose }: { onClose: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  const rfb = useRef<RFB | null>(null);
  const ticket = useRef<string | null>(null);
  const sound = useRef<HTMLAudioElement | null>(null);
  const keys = useRef<HTMLTextAreaElement>(null);
  const typed = useRef("");
  const [state, setState] = useState<ScreenState>("connecting");
  const [holding, setHolding] = useState(false);
  const [trouble, setTrouble] = useState<string | null>(null);

  const silence = () => {
    const audio = sound.current;
    sound.current = null;
    if (!audio) return;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
  };

  useEffect(() => {
    let gone = false;
    let close: (() => void) | null = null;
    void (async () => {
      const [{ ticket: opened }, { default: Rfb }] = await Promise.all([postJson<{ ticket: string }>("/api/screen/ticket"), import("@novnc/novnc")]);
      if (gone || !box.current) return;
      const url = `${BACKEND.replace(/^http/, "ws")}/api/screen?ticket=${encodeURIComponent(opened)}`;
      const view = new Rfb(box.current, url, { shared: true });
      view.viewOnly = true;
      view.scaleViewport = true;
      view.background = "transparent";
      view.addEventListener("connect", () => setState("on"));
      view.addEventListener("disconnect", () => {
        setState("off");
        setHolding(false);
        silence();
      });
      rfb.current = view;
      ticket.current = opened;
      close = () => view.disconnect();
    })().catch((err: unknown) => {
      console.error("screen: could not open it", err);
      if (!gone) setState("off");
    });
    return () => {
      gone = true;
      silence();
      close?.();
    };
  }, []);

  const takeOver = () => {
    const view = rfb.current;
    const open = ticket.current;
    if (!view || !open) return;
    setTrouble(null);
    // The sound starts inside the tap, since a phone lets audio start only
    // there; the backend holds the request until the lease is taken.
    const audio = new Audio(`${BACKEND}/api/screen/audio?ticket=${encodeURIComponent(open)}`);
    sound.current = audio;
    audio.play().catch((err: unknown) => console.warn("screen: no sound", err));
    postJson("/api/screen/lease", { ticket: open, take: true })
      .then(() => {
        view.viewOnly = false;
        view.focus();
        setHolding(true);
      })
      .catch(() => {
        silence();
        setTrouble("couldn't take it over");
      });
  };

  const handBack = () => {
    const view = rfb.current;
    const open = ticket.current;
    if (!view || !open) return;
    silence();
    view.viewOnly = true;
    setHolding(false);
    keys.current?.blur();
    postJson("/api/screen/lease", { ticket: open, take: false }).catch(() => setTrouble("couldn't hand it back"));
  };

  // What the field gained or lost since the last change goes to the screen
  // as keys, so the keyboard's own corrections come through as edits.
  const type = (e: FormEvent<HTMLTextAreaElement>) => {
    const view = rfb.current;
    if (!view) return;
    const now = e.currentTarget.value;
    const before = typed.current;
    let same = 0;
    while (same < now.length && same < before.length && now[same] === before[same]) same++;
    for (let i = same; i < before.length; i++) view.sendKey(BACKSPACE, "Backspace");
    for (const ch of now.slice(same)) {
      if (ch === "\n") view.sendKey(RETURN, "Enter");
      else view.sendKey(keysymOf(ch), null);
    }
    typed.current = now;
  };

  const label =
    trouble ??
    (state === "on" ? (holding ? "you're driving huvu's browser" : "huvu's browser, live") : state === "connecting" ? "a moment" : "huvu's browser isn't open right now");
  return (
    <m.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.4, ease: EASE }}
      className="absolute inset-0 z-40 flex flex-col gap-3 bg-paper/95 px-4 pb-4 pt-4 sm:px-10 sm:pt-6"
    >
      <div className="flex items-baseline gap-4">
        <span className={`${capsClass} ${trouble || state === "on" ? "text-sindoor" : "text-ivory/55"}`}>{label}</span>
        <span className="ml-auto" />
        {state === "on" && (
          <button type="button" onClick={holding ? handBack : takeOver} className={button}>
            {holding ? "hand back" : "take over"}
          </button>
        )}
        {state === "on" && holding && touch && (
          <button type="button" onClick={() => keys.current?.focus()} className={button}>
            keyboard
          </button>
        )}
        <button type="button" onClick={onClose} className={button}>
          close
        </button>
      </div>
      <div ref={box} className={`min-h-0 flex-1 overflow-hidden transition-shadow duration-500 ${holding ? "ring-2 ring-sindoor" : ""}`} />
      {touch && (
        <textarea
          ref={keys}
          aria-label="type into huvu's browser"
          autoCapitalize="off"
          autoComplete="off"
          autoCorrect="off"
          spellCheck={false}
          onInput={type}
          onBlur={(e) => {
            e.currentTarget.value = "";
            typed.current = "";
          }}
          className="pointer-events-none absolute h-px w-px opacity-0"
        />
      )}
    </m.div>
  );
}
