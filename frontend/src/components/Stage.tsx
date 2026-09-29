import { useEffect, useEffectEvent, useLayoutEffect, useMemo, useRef, useState } from "react";
import { App as NativeApp } from "@capacitor/app";
import { Capacitor } from "@capacitor/core";
import * as m from "motion/react-m";
import { AnimatePresence, LayoutGroup } from "motion/react";
import { growForWords } from "../canvas/garden";
import * as thread from "../chat/thread";
import { initialMode, TARGET, TV, ui, type Mode } from "../config/interaction";
import { launch, snapshot, type Snapshot } from "../stage/flight";
import { capsClass, EASE } from "../stage/style";
import { useScrollIntent } from "../stage/useScrollIntent";
import { useSlotGeometry } from "../stage/useSlotGeometry";
import { useStable } from "../stage/useStable";
import { useWide } from "../stage/useWide";
import { useVoiceSession, type VoicePhase } from "../voice/useVoiceSession";
import { Composer } from "./Composer";
import { TurnView } from "./TurnView";
import { VoiceFlower, type FlowerState } from "./VoiceFlower";

// The whole UI over the garden. Not a chat log: the stage holds one turn
// at a time, big. Scrolling up folds it into a history of every turn at
// one size; picking a row makes that turn the stage again, and any scroll
// folds it back. Voice mode moves the flower to the middle and hides the
// text field; the thread and the history stay the same underneath.
type View = { kind: "stage"; focus: string | null } | { kind: "history"; cursor: string | null };

// Listening starts the moment a push-to-talk key goes down, so the first
// syllable is never lost. The screen only switches to voice mode once the
// key has been held this long, so a quick tap on space changes nothing.
const HOLD_MS = 220;
// The flower is drawn once, at its voice-mode size, and scaled down to sit
// in the composer, so it stays crisp in the middle of the screen.
const FLOWER_BIG = ui.flowerSize;
const FLOWER_SMALL = 56;
// Words of one written reply that may grow the garland: about one flower.
const WRITE_WORDS_MAX = 85;

const PHASE_HINT: Record<Exclude<VoicePhase, "idle">, string> = {
  listening: "listening",
  thinking: "thinking",
  speaking: "huvu speaks / talk over to interrupt",
};

const PHASE_FLOWER: Record<VoicePhase, FlowerState> = {
  idle: "dancing",
  listening: "listening",
  thinking: "thinking",
  speaking: "speaking",
};

export function Stage() {
  const { turns: allTurns, running } = thread.useThread();
  const wide = useWide();

  const [rewindId, setRewindId] = useState<string | null>(null);
  // While rewinding, the stage pretends the chosen turn and everything
  // after it never happened. Sending replaces them for real.
  const turns = useMemo(() => {
    const cut = rewindId ? allTurns.findIndex((t) => t.id === rewindId) : -1;
    return cut < 0 ? allTurns : allTurns.slice(0, cut);
  }, [allTurns, rewindId]);

  const [view, setView] = useState<View>({ kind: "stage", focus: null });
  const [mode, setMode] = useState<Mode>(initialMode);
  const [draft, setDraft] = useState("");
  // Turns asked before this moment (seeded or restored) never grow the
  // garland and are never announced.
  const [sessionStart] = useState(() => Date.now());
  const rootRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);
  const slotRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLOListElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const transcriptRef = useRef<HTMLParagraphElement>(null);
  const focusables = useRef(new Map<string, { open?: HTMLButtonElement | null; edit?: HTMLButtonElement | null }>());
  const ptt = useRef<{ timer: number; holding: boolean } | null>(null);
  const wrote = useRef({ id: "", words: 0, sent: 0 });
  const geo = useSlotGeometry(rootRef, slotRef, composerRef);

  const latest = turns.at(-1) ?? null;
  const heroId = view.kind === "stage" ? (turns.some((t) => t.id === view.focus) ? view.focus : (latest?.id ?? null)) : null;
  const shown = view.kind === "history" ? turns : turns.filter((t) => t.id === heroId);

  const submit = (text: string, from?: Snapshot) => {
    const clean = text.trim();
    if (!clean) return;
    if (from) launch("ask", clean, from);
    if (rewindId) {
      thread.edit(rewindId, clean);
      setRewindId(null);
    } else {
      thread.ask(clean);
    }
    setView({ kind: "stage", focus: null });
  };

  const voice = useVoiceSession((text) => submit(text, transcriptRef.current ? snapshot(transcriptRef.current) : undefined));

  const toStage = (focus: string | null) => {
    if (TV) (document.activeElement as HTMLElement | null)?.blur();
    setView({ kind: "stage", focus });
  };
  const toHistory = (cursor: string | null) => setView({ kind: "history", cursor: cursor ?? latest?.id ?? null });

  const leaveVoice = () => {
    if (!ui.modeSwitchable) return;
    voice.exit();
    setMode("text");
  };

  const onEdit = (turn: thread.Turn, from: Snapshot) => {
    setRewindId(turn.id);
    setView({ kind: "stage", focus: null });
    // Fire TV has no text to edit. Rewinding there means the next thing
    // said replaces this turn.
    if (TV) return;
    if (mode === "voice") leaveVoice();
    launch("edit", turn.ask, from);
    setDraft(turn.ask);
    // Focus after the text has landed. On phone, focusing opens the
    // keyboard and shifts the layout, which would move the landing spot
    // mid-flight.
    window.setTimeout(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.focus({ preventScroll: true });
      el.setSelectionRange(el.value.length, el.value.length);
    }, 800);
  };

  const cancelRewind = () => {
    setRewindId(null);
    setDraft("");
  };

  const stop = thread.stop;

  // Undoes the innermost thing: a reply still coming in, a rewind, history
  // or an opened turn, then voice mode. Returns false when there is
  // nothing left to undo (the hardware back button then leaves the app).
  const goBack = (): boolean => {
    if (running && mode === "text") stop();
    else if (rewindId) cancelRewind();
    else if (view.kind === "history" || view.focus) toStage(null);
    else if (mode === "voice" && ui.modeSwitchable) leaveVoice();
    else return false;
    return true;
  };

  // TurnView, Composer and VoiceFlower are memoised: a streaming reply
  // re-renders the stage on every chunk, and only the turn it belongs to
  // should follow.
  const turnActions = useStable({
    open: (id: string) => toStage(id),
    edit: onEdit,
    choose: (text: string) => submit(text),
    retry: (turn: thread.Turn) => thread.retry(turn.id),
    registerFocus: (id: string, which: "open" | "edit", el: HTMLButtonElement | null) => {
      const entry = focusables.current.get(id) ?? {};
      entry[which] = el;
      focusables.current.set(id, entry);
    },
  });
  const composerActions = useStable({
    submit: (text: string, from: Snapshot) => {
      setDraft("");
      submit(text, from);
    },
    cancelRewind,
    stop,
  });
  const { onFlower } = useStable({
    onFlower: () => {
      if (mode === "voice") {
        leaveVoice();
      } else {
        textareaRef.current?.blur();
        setMode("voice");
        voice.begin(true);
      }
    },
  });

  // Scrolling up folds the stage into history; in history, scrolling past
  // the bottom brings the latest turn back. A turn opened from history
  // folds back on a scroll either way.
  const scroll = useScrollIntent(listRef, (up) => {
    if (view.kind === "history") {
      if (!up) toStage(null);
    } else if (up || heroId !== latest?.id) {
      toHistory(heroId);
    }
  });

  const moveCursor = (step: number) => {
    if (view.kind !== "history") return;
    const next = turns.findIndex((t) => t.id === view.cursor) + step;
    if (next >= turns.length) toStage(null);
    else if (next >= 0) setView({ kind: "history", cursor: turns[next].id });
  };

  // Fire TV: up and down walk the history, right reaches the rewind
  // button of the turn in focus, left comes back.
  const onTvArrow = (e: KeyboardEvent) => {
    const current = view.kind === "history" ? view.cursor : heroId;
    const moves: Record<string, () => void> = {
      ArrowUp: () => (view.kind === "stage" ? toHistory(heroId) : moveCursor(-1)),
      ArrowDown: () => (view.kind === "stage" ? heroId !== latest?.id && toHistory(heroId) : moveCursor(1)),
      ArrowRight: () => current && focusables.current.get(current)?.edit?.focus(),
      ArrowLeft: () =>
        view.kind === "history" && current
          ? focusables.current.get(current)?.open?.focus()
          : (document.activeElement as HTMLElement | null)?.blur(),
    };
    const move = moves[e.key];
    if (!move) return;
    e.preventDefault();
    move();
  };

  const onKeyDown = useEffectEvent((e: KeyboardEvent) => {
    const t = e.target instanceof HTMLElement ? e.target : null;
    const inField = t?.tagName === "TEXTAREA" || t?.tagName === "INPUT" || !!t?.isContentEditable;

    if (ui.pushToTalkKey && e.key === ui.pushToTalkKey) {
      if (ptt.current) {
        e.preventDefault();
        return;
      }
      // Web: space talks unless it would type into a draft. Fire TV:
      // select talks on the stage; in history it picks the focused row.
      const eligible = !e.repeat && t?.tagName !== "BUTTON" && (TV ? view.kind === "stage" : !inField || draft === "");
      if (eligible) {
        e.preventDefault();
        if (!voice.hold()) return;
        const timer = window.setTimeout(() => {
          if (!ptt.current) return;
          ptt.current.holding = true;
          textareaRef.current?.blur();
          setMode("voice");
        }, HOLD_MS);
        ptt.current = { timer, holding: false };
        return;
      }
    }

    if (e.key === "Escape" || e.key === "GoBack" || e.key === "BrowserBack") {
      goBack();
      return;
    }

    if (TV) return onTvArrow(e);

    // Web: typing always lands in the field, wherever focus happens to be.
    // In voice mode it also brings the field back.
    const printable = e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
    if (TARGET !== "web" || inField || !printable) return;
    if (mode === "voice") {
      e.preventDefault();
      leaveVoice();
      setDraft((d) => d + e.key);
      return;
    }
    // Focusing during keydown sends this very key to the field.
    textareaRef.current?.focus({ preventScroll: true });
  });

  const releasePtt = useEffectEvent(() => {
    if (!ptt.current) return;
    const { timer, holding } = ptt.current;
    ptt.current = null;
    window.clearTimeout(timer);
    if (holding) voice.release();
    else voice.dropHold();
  });

  useEffect(() => {
    const down = (e: KeyboardEvent) => onKeyDown(e);
    const up = (e: KeyboardEvent) => {
      if (e.key === ui.pushToTalkKey) releasePtt();
    };
    // A key released while the window is in the background never fires
    // keyup, so losing focus counts as letting go.
    const blur = () => releasePtt();
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", blur);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      window.removeEventListener("blur", blur);
    };
  }, []);

  // Android (phone and Fire TV): the hardware back button never reaches
  // the page as a key, only through Capacitor. It undoes the way Escape
  // does, and leaves the app once there is nothing left to undo.
  const onBackButton = useEffectEvent(() => {
    if (!goBack()) NativeApp.exitApp();
  });
  useEffect(() => {
    if (!Capacitor.isNativePlatform()) return;
    const listener = NativeApp.addListener("backButton", () => onBackButton());
    return () => {
      listener.then((l) => l.remove());
    };
  }, []);

  // A written reply grows the garland a little per new word. In voice mode
  // huvu's voice grows it instead.
  useEffect(() => {
    const turn = allTurns.at(-1);
    if (!turn || turn.at < sessionStart || mode !== "text") return;
    const w = wrote.current.id === turn.id ? wrote.current : { id: turn.id, words: 0, sent: 0 };
    const words = turn.answer.split(/\s+/).filter(Boolean).length;
    const grow = Math.max(0, Math.min(words - w.words, WRITE_WORDS_MAX - w.sent));
    if (grow > 0) growForWords(grow);
    wrote.current = { id: turn.id, words, sent: w.sent + grow };
  }, [allTurns, mode, sessionStart]);

  // Web: the text field is always the one in focus while in text mode, so
  // typing and holding space both work without a click first. Phone is
  // left alone, since focusing there pops up the keyboard.
  useEffect(() => {
    if (TARGET === "web" && mode === "text") textareaRef.current?.focus({ preventScroll: true });
  }, [mode, view]);

  // A deep link with ?mode=voice starts listening at once.
  const startInitialVoice = useEffectEvent(() => {
    if (mode === "voice" && !TV) voice.begin(true);
  });
  useEffect(() => startInitialVoice(), []);

  // History opens scrolled to the newest turn.
  useLayoutEffect(() => {
    const list = listRef.current;
    if (list && view.kind === "history") list.scrollTop = list.scrollHeight;
  }, [view.kind]);

  useEffect(() => {
    if (!TV || view.kind !== "history" || !view.cursor) return;
    const el = focusables.current.get(view.cursor)?.open;
    el?.focus({ preventScroll: true });
    el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [view]);

  const inVoice = mode === "voice";
  const hint = voice.phase === "idle" ? (rewindId && TV ? "rewound. hold select to ask again" : ui.idleHint) : PHASE_HINT[voice.phase];
  // Browsing history in voice mode puts the flower away: it shrinks and
  // fades where it is, and the list takes the whole height. Opening a turn
  // (or scrolling back down) brings it back.
  const flowerAway = inVoice && view.kind === "history";
  const voiceStage = inVoice && !flowerAway;
  // Voice mode: the flower sits in the middle and the turn stays above it.
  // Text mode: the flower sits in the composer and the turn fills the rest.
  const flowerAt =
    geo &&
    (inVoice
      ? { x: geo.w / 2 - FLOWER_BIG / 2, y: geo.h / 2 - FLOWER_BIG / 2, scale: flowerAway ? 0.35 : 1, opacity: flowerAway ? 0 : 1 }
      : { x: geo.slotX - FLOWER_BIG / 2, y: geo.slotY - FLOWER_BIG / 2, scale: FLOWER_SMALL / FLOWER_BIG, opacity: 1 });
  // In voice history the list stops just above the "write" button.
  const listBottom = !geo ? 0 : voiceStage ? geo.h / 2 + FLOWER_BIG / 2 : inVoice ? 88 : geo.composerH;
  // Screen readers hear each answer once, when it is complete.
  const announcement = latest && latest.at >= sessionStart && !latest.running ? latest.answer : "";

  return (
    <div ref={rootRef} data-target={TARGET} {...scroll} className="relative z-10 h-dvh w-full overflow-hidden text-ivory">
      {/* The turn hangs from the top of the stage, so a streaming answer
          grows downward and never pushes its YOU cue around. History
          anchors its rows to the bottom instead, newest last. */}
      <LayoutGroup>
        <m.ol
          ref={listRef}
          layoutScroll
          style={{ bottom: listBottom }}
          className="list-fade absolute inset-x-0 top-0 m-0 mx-auto flex w-full max-w-[52rem] list-none flex-col gap-9 overflow-y-auto px-6 pb-8 pt-[16dvh] [scrollbar-width:none] sm:px-12"
        >
          <li aria-hidden="true" className={view.kind === "history" ? "mt-auto" : ""} />
          <AnimatePresence mode="popLayout" initial={false}>
            {shown.map((turn) => (
              <TurnView key={turn.id} turn={turn} variant={view.kind === "history" ? "row" : "hero"} wide={wide} actions={turnActions} />
            ))}
            {turns.length === 0 && (
              <m.li
                key="empty"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0, transition: { duration: 0.35, ease: EASE } }}
                transition={{ duration: 0.8, ease: EASE }}
                className="halo font-bodoni text-[34px] italic leading-[1.1] text-ivory/40 sm:text-[46px]"
              >
                what do you need?
              </m.li>
            )}
          </AnimatePresence>
        </m.ol>
      </LayoutGroup>

      <p aria-live="polite" className="sr-only">
        {announcement}
      </p>

      <span className={`${capsClass} halo pointer-events-none absolute left-6 top-7 z-20 text-[11px] tracking-[0.42em] text-ivory/75 sm:left-12 sm:top-10`}>
        huvu
      </span>

      <m.div
        aria-live="polite"
        initial={false}
        animate={{ opacity: voiceStage ? 1 : 0 }}
        transition={{ duration: 0.6, ease: EASE }}
        inert={!voiceStage}
        style={{ top: `calc(50% + ${FLOWER_BIG / 2 + 20}px)` }}
        className={`absolute inset-x-0 z-20 flex flex-col items-center ${voiceStage ? "" : "pointer-events-none"}`}
      >
        <p
          ref={transcriptRef}
          className="halo m-0 min-h-[1.4em] max-w-[34rem] px-6 text-center font-display text-[20px] font-medium leading-[1.4] text-ivory/90 sm:text-[24px]"
        >
          {voice.transcript}
        </p>
        <AnimatePresence mode="wait" initial={false}>
          <m.p
            key={hint}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.4, ease: EASE }}
            className={`${capsClass} halo m-0 mt-4 text-ivory/60`}
          >
            {hint}
          </m.p>
        </AnimatePresence>
      </m.div>

      {ui.modeSwitchable && (
        <m.button
          type="button"
          onClick={leaveVoice}
          initial={false}
          animate={{ opacity: inVoice ? 1 : 0 }}
          transition={{ duration: 0.6, ease: EASE }}
          inert={!inVoice}
          className={`${capsClass} absolute bottom-6 left-6 z-20 flex min-h-11 items-center gap-3 text-ivory/70 transition-colors duration-500 hover:text-ivory sm:left-12 ${inVoice ? "" : "pointer-events-none"}`}
        >
          <span className="h-px w-5 bg-thread" />
          write
        </m.button>
      )}

      {!TV && (
        <Composer
          ref={composerRef}
          slotRef={slotRef}
          textareaRef={textareaRef}
          shown={!inVoice}
          draft={draft}
          onDraft={setDraft}
          onSubmit={composerActions.submit}
          rewinding={rewindId !== null}
          onCancelRewind={composerActions.cancelRewind}
          running={running}
          onStop={composerActions.stop}
        />
      )}

      {/* One flower for the whole app, above everything. Switching modes
          glides it between the composer and the middle of the screen with
          a transform; it never unmounts, so it never blinks. */}
      {flowerAt && (
        <m.div
          initial={false}
          animate={flowerAt}
          transition={{ duration: 0.9, ease: EASE, opacity: { duration: 0.5, ease: EASE } }}
          inert={flowerAway}
          style={{ width: FLOWER_BIG, height: FLOWER_BIG }}
          className={`absolute left-0 top-0 z-30 ${flowerAway ? "pointer-events-none" : ""}`}
        >
          <VoiceFlower
            state={inVoice ? PHASE_FLOWER[voice.phase] : "bud"}
            size={FLOWER_BIG}
            level={voice.level}
            label={inVoice ? "back to writing" : "speak to huvu"}
            onClick={TV ? undefined : onFlower}
            focusable={!TV}
          />
        </m.div>
      )}
    </div>
  );
}
