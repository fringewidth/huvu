import { lazy, Suspense, useEffect, useEffectEvent, useLayoutEffect, useMemo, useRef, useState } from "react";
import { App as NativeApp } from "@capacitor/app";
import { Capacitor } from "@capacitor/core";
import * as m from "motion/react-m";
import { AnimatePresence, LayoutGroup } from "motion/react";
import { growForWords } from "../canvas/host";
import { onOpens, openedWake } from "../account/opens";
import { useReach } from "../account/session";
import { useWaiting } from "../account/waiting";
import { refreshFeeds, type Feed } from "../chat/feeds";
import * as thread from "../chat/thread";
import { endWake, leaveWake, showWake, useWakes, type WakeInfo } from "../chat/wakes";
import { initialMode, PHONE, TARGET, TV, ui, type Mode } from "../config/interaction";
import { launch, snapshot, type Snapshot } from "../stage/flight";
import { capsClass, EASE } from "../stage/style";
import { useScrollIntent } from "../stage/useScrollIntent";
import { useSlotGeometry } from "../stage/useSlotGeometry";
import { useStable } from "../stage/useStable";
import { useWide } from "../stage/useWide";
import { speaking, stopSpeaking } from "../voice/speaker";
import { isWikiOpen, onWikiChange, useWikiOpen, wikiBack } from "../wiki/wiki";
import { useSpokenAnswers } from "../voice/useSpokenAnswers";
import { useVoiceSession, type VoicePhase } from "../voice/useVoiceSession";
import { onWakeWord } from "../voice/wakeWord";
import { isClipsOpen, onClipsChange, setClipsOpen, useClipsOpen } from "../voice/clips";
import { Composer } from "./Composer";
import { Feeds } from "./Feeds";
import { Menu, type MenuSection } from "./Menu";
import { TurnView } from "./TurnView";
import { Waiting } from "./Waiting";
import { VoiceFlower, type FlowerState } from "./VoiceFlower";

// The wiki, and its Markdown and editor libraries, load the first time it
// opens.
const WikiView = lazy(() => import("./Wiki").then((mod) => ({ default: mod.WikiView })));
const VoiceClipsView = lazy(() => import("./VoiceClips").then((mod) => ({ default: mod.VoiceClipsView })));
// huvu's browser, live, and noVNC with it, load the first time its screen
// is watched.
const ScreenView = lazy(() => import("./ScreenView").then((mod) => ({ default: mod.ScreenView })));

declare global {
  interface Window {
    huvuBack?: () => boolean;
  }
}

// The whole UI over the garden. Not a chat log: the stage holds one turn
// at a time, big. Scrolling up folds it into a history of every turn at
// one size; picking a row makes that turn the stage again, and any scroll
// folds it back. Voice mode moves the flower to the middle and hides the
// text field; the thread and the history stay the same underneath.
//
// At launch the stage shows the greeting, with the turns from before (the
// backend's, and notices) waiting in history. A turn takes the stage when
// it is asked in this session, is still running, or is a notice nobody
// has seen yet.
type View = { kind: "stage"; focus: string | null } | { kind: "history"; cursor: string | null };

// Listening starts the moment a push-to-talk key goes down, so the first
// syllable is never lost. The screen only switches to voice mode once the
// key has been held this long, so a quick tap on space changes nothing.
const HOLD_MS = 220;
// The flower is drawn once, at its voice-mode size, and scaled down to sit
// in the composer, so it stays crisp in the middle of the screen.
// Fire TV has no flower, and its layout never reads this.
const FLOWER_BIG = ui.flowerSize ?? 0;
const FLOWER_SMALL = 56;
// Words of one written reply that may grow the garland: about one flower.
const WRITE_WORDS_MAX = 85;
// Where the docked flower's center sits above the bottom edge: level with
// the "write" button.
const DOCK_Y = 46;

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
  const { turns: allTurns, running, stopping } = thread.useThread();
  // A wake on the stage (chat/wakes.ts): the thread is its turns, and what
  // the user sends goes to it. An ended one can still be read, and for a
  // day after its run ended it, answered.
  const { shown: wake } = useWakes();
  const wakeEnded = wake?.status === "ended" && !wake.answerable;
  const canAsk = ui.hasInput && !wakeEnded;
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
  // Voice mode: scrolling the stage docks the flower, small, at the bottom,
  // so a long answer gets the screen. Tapping it, pressing the talk key, or
  // speaking while it listens brings it back.
  const [docked, setDocked] = useState(false);
  const [draft, setDraft] = useState("");
  // Asks typed while a reply still comes in. They go as one ask once it
  // ends. Stopping it puts them back into the field, and so does a reply
  // that failed: a phone back from the background can lose a turn's
  // stream while the turn still runs, and a new ask would stop it.
  const [queued, setQueued] = useState<string[]>([]);
  const unqueued = (text: string) => [...queued, text].filter((t) => t.trim()).join("\n");
  const [feedsOpen, setFeedsOpen] = useState(false);
  const [menu, setMenu] = useState<MenuSection | null>(null);
  const wikiOpen = useWikiOpen();
  const clipsOpen = useClipsOpen();
  // huvu's browser screen, over the stage.
  const [screenOpen, setScreenOpen] = useState(false);
  // How tall the card of what waits on this phone is, so the turn stops
  // short of it.
  const [waitingH, setWaitingH] = useState(0);
  // Turns asked before this moment (seeded or restored) never grow the
  // garland and are never announced.
  const [sessionStart] = useState(() => Date.now());
  const [greeting] = useState(() => ui.greetings[Math.floor(Math.random() * ui.greetings.length)]);
  const rootRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);
  const slotRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLOListElement>(null);
  const feedsRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const transcriptRef = useRef<HTMLParagraphElement>(null);
  // Each history row's open button, for Fire TV's D-pad.
  const rowButtons = useRef(new Map<string, HTMLButtonElement | null>());
  const ptt = useRef<{ timer: number; holding: boolean } | null>(null);
  const wrote = useRef({ id: "", words: 0, sent: 0 });
  // Turns that took the stage once. They keep it after they end, even if
  // they began before this launch.
  const [staged, setStaged] = useState<ReadonlySet<string>>(() => new Set());
  const geo = useSlotGeometry(rootRef, slotRef, composerRef, canAsk);

  // Another conversation took the stage: it starts on its latest turn,
  // with nothing half done from the one before.
  const wakeId = wake?.id ?? null;
  const [stagedWake, setStagedWake] = useState<string | null>(null);
  if (stagedWake !== wakeId) {
    setStagedWake(wakeId);
    setScreenOpen(false);
    setMenu(null);
    setRewindId(null);
    setView({ kind: "stage", focus: null });
  }

  const latest = turns.at(-1) ?? null;
  // A wake opens on its latest turn, however old.
  if (latest && !staged.has(latest.id) && (latest.at >= sessionStart || latest.running || latest.notice?.unseen || wake)) setStaged(new Set([...staged, latest.id]));
  const latestStaged = latest && staged.has(latest.id) ? latest.id : null;
  const heroId = view.kind === "stage" ? (turns.some((t) => t.id === view.focus) ? view.focus : latestStaged) : null;
  const shown = view.kind === "history" ? turns : turns.filter((t) => t.id === heroId);

  // How an ask is made. A screen that says every answer (Fire TV) sends
  // every ask as spoken, a choice picked with the remote too, so the
  // answer comes ready to be said.
  const inputMode: thread.InputMode = mode === "voice" || ui.speaksAnswers ? "voice" : "text";
  const submit = (text: string, from?: Snapshot) => {
    const clean = text.trim();
    if (!clean) return;
    if (from) launch("ask", clean, from);
    if (rewindId) {
      thread.edit(rewindId, clean, inputMode);
      setRewindId(null);
    } else {
      thread.ask(clean, inputMode);
    }
    setView({ kind: "stage", focus: null });
  };

  const voice = useVoiceSession((text) => submit(text, transcriptRef.current ? snapshot(transcriptRef.current) : undefined));
  useSpokenAnswers(ui.speaksAnswers, sessionStart);
  if (docked && (voice.heard || voice.transcript)) setDocked(false);
  const dock = () => {
    if (mode === "voice" && view.kind === "stage") setDocked(true);
  };

  const toStage = (focus: string | null) => {
    if (TV) (document.activeElement as HTMLElement | null)?.blur();
    setView({ kind: "stage", focus });
  };
  const toHistory = (cursor: string | null) => setView({ kind: "history", cursor: cursor ?? latest?.id ?? null });

  const leaveVoice = () => {
    if (!ui.modeSwitchable) return;
    voice.exit();
    setMode("text");
    setDocked(false);
  };

  const onEdit = (turn: thread.Turn, from: Snapshot) => {
    setRewindId(turn.id);
    setView({ kind: "stage", focus: null });
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

  const unqueue = () => {
    if (queued.length === 0) return;
    setDraft(unqueued(draft));
    setQueued([]);
  };

  // The queue goes once the reply has finished, through `outbox`, since
  // asking is no state of this component's. It waits out a rewind, and
  // goes after the edit's reply. It goes back into the field in voice
  // mode, which talks over the reply, and when another conversation takes
  // the stage, since it belongs to the one it was typed in.
  const [outbox, setOutbox] = useState<{ n: number; text: string } | null>(null);
  if (queued.length > 0) {
    if (mode === "voice" || stagedWake !== wakeId) unqueue();
    else if (!running && !rewindId) {
      if (allTurns.findLast(thread.asked)?.ending === "failed") unqueue();
      else {
        setOutbox({ n: (outbox?.n ?? 0) + 1, text: queued.join("\n") });
        setQueued([]);
      }
    }
  }

  const stop = () => {
    thread.stop();
    unqueue();
  };

  // Undoes the innermost thing: a reply still coming in, a rewind, history
  // or an opened turn, then voice mode. Returns false when there is
  // nothing left to undo (the hardware back button then leaves the app).
  const goBack = (): boolean => {
    if (isWikiOpen()) wikiBack();
    else if (isClipsOpen()) setClipsOpen(false);
    else if (screenOpen) setScreenOpen(false);
    else if (menu) setMenu(null);
    else if (feedsOpen) setFeedsOpen(false);
    // A screen that says every answer: back quiets huvu, and stops the
    // reply if it is still coming in.
    else if (ui.speaksAnswers && (speaking() || (running && !stopping))) {
      stop();
      stopSpeaking();
    } else if (running && !stopping && mode === "text") stop();
    else if (rewindId) cancelRewind();
    else if (view.kind === "history" || view.focus) toStage(null);
    else if (wake) leaveWake();
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
    // Text mode sends nothing new while a reply is still coming in.
    choose: (text: string) => {
      if (mode === "text" && running) return;
      submit(text);
    },
    retry: (turn: thread.Turn) => thread.retry(turn.id, inputMode),
    show: (section: MenuSection) => setMenu(section),
    openWake: (id: string) => void showWake(id),
    watch: () => setScreenOpen(true),
    registerFocus: (id: string, el: HTMLButtonElement | null) => {
      rowButtons.current.set(id, el);
    },
  });
  const feedsActions = useStable({
    toggle: () => setFeedsOpen((open) => !open),
    // Watching asks huvu nothing, so it works even while a reply is
    // still coming in. A rewind in progress is dropped, since the feed's
    // turn lands after the turns it hides.
    pick: (feed: Feed) => {
      setFeedsOpen(false);
      setRewindId(null);
      thread.watch(feed);
      setView({ kind: "stage", focus: null });
    },
  });
  const menuActions = useStable({
    toggle: () => setMenu((open) => (open ? null : "top")),
    close: () => setMenu(null),
  });
  const composerActions = useStable({
    submit: (text: string, from: Snapshot) => {
      setDraft("");
      if (running && !rewindId) setQueued([...queued, text]);
      else submit(text, from);
    },
    cancelRewind,
    stop,
    unqueue,
  });
  // The flower always listens, and cuts huvu off to do it. The "write"
  // button is the way back to text. Tapping the docked flower only brings
  // it back to the middle.
  const { onFlower } = useStable({
    onFlower: () => {
      if (docked) {
        setDocked(false);
        return;
      }
      if (voice.phase === "listening") return;
      textareaRef.current?.blur();
      setMode("voice");
      voice.begin();
    },
  });

  // Scrolling up folds the stage into history; in history, scrolling past
  // the bottom brings the latest turn back. A turn opened from history
  // folds back on a scroll either way.
  const scroll = useScrollIntent(listRef, (up) => {
    if (view.kind === "history") {
      if (!up) toStage(null);
    } else if (up || (heroId !== null && heroId !== latest?.id)) {
      toHistory(heroId);
    }
  }, dock);

  const moveCursor = (step: number) => {
    if (view.kind !== "history") return;
    const next = turns.findIndex((t) => t.id === view.cursor) + step;
    if (next >= turns.length) toStage(null);
    else if (next >= 0) setView({ kind: "history", cursor: turns[next].id });
  };

  // Fire TV has no scroll gesture, so on the stage up and down scroll the
  // list a page at a time first, the way a scroll does on web and phone:
  // a long answer and the panels under it stay in reach. Only at the
  // list's edge do they leave the stage.
  const scrollList = (up: boolean) => {
    const list = listRef.current;
    if (!list) return false;
    const room = up ? list.scrollTop : list.scrollHeight - list.clientHeight - list.scrollTop;
    if (room < 1) return false;
    list.scrollBy({ top: (up ? -0.7 : 0.7) * list.clientHeight, behavior: "smooth" });
    dock();
    return true;
  };

  // Fire TV, with the feeds list open: up and down walk its rows that can
  // be watched, and select picks one.
  const onTvFeedsArrow = (e: KeyboardEvent) => {
    if (!e.key.startsWith("Arrow")) return;
    e.preventDefault();
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    const rows = [...(feedsRef.current?.querySelectorAll<HTMLButtonElement>("button:enabled") ?? [])];
    const at = rows.indexOf(document.activeElement as HTMLButtonElement);
    rows[Math.max(0, Math.min(rows.length - 1, at + (e.key === "ArrowUp" ? -1 : 1)))]?.focus();
  };

  // Fire TV: the choices under the turn on the stage. Down reaches them
  // once the turn has scrolled to its end, left and right walk them, up
  // leaves them, and select sends the one in focus.
  const choiceButtons = () => [...(rootRef.current?.querySelectorAll<HTMLButtonElement>("[data-choice]") ?? [])];
  const reachChoices = () => {
    const buttons = choiceButtons();
    if (buttons.length === 0) return false;
    if (!buttons.includes(document.activeElement as HTMLButtonElement)) buttons[0].focus();
    return true;
  };
  const stepChoice = (by: number) => {
    const buttons = choiceButtons();
    const at = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (at < 0) return false;
    buttons[Math.max(0, Math.min(buttons.length - 1, at + by))].focus();
    return true;
  };
  const leaveChoices = () => {
    if (!choiceButtons().includes(document.activeElement as HTMLButtonElement)) return false;
    (document.activeElement as HTMLElement).blur();
    return true;
  };

  // Fire TV: up and down walk the history, and select opens the row in
  // focus.
  const onTvArrow = (e: KeyboardEvent) => {
    if (feedsOpen) return onTvFeedsArrow(e);
    const stage = view.kind === "stage";
    const moves: Record<string, () => void> = {
      ArrowUp: () => (stage ? leaveChoices() || scrollList(true) || toHistory(heroId) : moveCursor(-1)),
      ArrowDown: () => (stage ? scrollList(false) || reachChoices() || (heroId !== null && heroId !== latest?.id && toHistory(heroId)) : moveCursor(1)),
      ArrowLeft: () => stage && stepChoice(-1),
      ArrowRight: () => stage && stepChoice(1),
    };
    const move = moves[e.key];
    if (!move) return;
    e.preventDefault();
    move();
  };

  const onKeyDown = useEffectEvent((e: KeyboardEvent) => {
    // The wiki has keys of its own, its editor among them, and the
    // recording screen must not start push-to-talk: only back reaches the
    // stage.
    if (isWikiOpen() || isClipsOpen()) {
      if (e.key === "Escape" || e.key === "GoBack" || e.key === "BrowserBack") goBack();
      return;
    }
    const t = e.target instanceof HTMLElement ? e.target : null;
    const inField = t?.tagName === "TEXTAREA" || t?.tagName === "INPUT" || !!t?.isContentEditable;

    if (ui.pushToTalkKey && e.key === ui.pushToTalkKey) {
      if (ptt.current) {
        e.preventDefault();
        return;
      }
      // Space talks unless it would type into a draft or press a button.
      // The flower is the one button it talks on.
      const button = t?.tagName === "BUTTON" && t.dataset.talk === undefined;
      const eligible = !e.repeat && !button && (!inField || draft === "");
      if (eligible) {
        e.preventDefault();
        // Voice mode talks over a reply still coming in at once. Text mode
        // cuts it off only once the press is a real hold, so a tap of
        // space leaves it running.
        const later = mode === "text" && running;
        setDocked(false);
        voice.hold(!later);
        // Held in history, the latest turn comes back to the stage, so
        // what is heard and what huvu answers have somewhere to show.
        const timer = window.setTimeout(() => {
          if (!ptt.current) return;
          if (later) voice.interrupt();
          ptt.current.holding = true;
          textareaRef.current?.blur();
          setMode("voice");
          if (view.kind === "history") toStage(null);
        }, HOLD_MS);
        ptt.current = { timer, holding: false };
        return;
      }
    }

    if (e.key === "Escape" || e.key === "GoBack" || e.key === "BrowserBack") {
      goBack();
      return;
    }

    if (ui.feedsKey && e.key === ui.feedsKey) {
      e.preventDefault();
      setFeedsOpen((open) => !open);
      return;
    }

    if (TV) return onTvArrow(e);

    // Web: typing always lands in the field, wherever focus happens to be.
    // In voice mode it also brings the field back.
    const printable = e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
    if (!ui.keyboard || inField || !printable) return;
    if (mode === "voice") {
      e.preventDefault();
      leaveVoice();
      setDraft((d) => d + e.key);
      return;
    }
    // Focusing during keydown sends this very key to the field.
    textareaRef.current?.focus({ preventScroll: true });
  });

  // Whether there was a hold to let go of.
  const releasePtt = useEffectEvent((): boolean => {
    if (!ptt.current) return false;
    const { timer, holding } = ptt.current;
    ptt.current = null;
    window.clearTimeout(timer);
    if (holding) voice.release();
    else voice.dropHold();
    return true;
  });

  useEffect(() => {
    const down = (e: KeyboardEvent) => onKeyDown(e);
    const up = (e: KeyboardEvent) => {
      if (e.key !== ui.pushToTalkKey) return;
      // A focused button presses on space's keyup. Talking was the press.
      if (releasePtt()) e.preventDefault();
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

  // The Fire TV app (frontend/android/tv) is a plain WebView, not
  // Capacitor. It asks the page what back does, and closes once there is
  // nothing left to undo.
  const onShellBack = useEffectEvent(() => goBack());
  useEffect(() => {
    if (!TV) return;
    window.huvuBack = () => onShellBack();
    return () => {
      delete window.huvuBack;
    };
  }, []);

  // A tapped notification that names a screen opens it: a wake takes the
  // stage, and anything else opens the menu there.
  useEffect(
    () =>
      onOpens((opens) => {
        const id = openedWake(opens);
        if (id) void showWake(id);
        else setMenu(opens as MenuSection);
      }),
    [],
  );



  // The recording screen takes the mic, and must hear only the person:
  // voice mode ends, and huvu goes quiet.
  const clipsOpened = useEffectEvent(() => {
    setMenu(null);
    if (mode === "voice") leaveVoice();
    stopSpeaking();
  });

  // The menu opens the wiki, and huvu the recording screen, which takes
  // the menu's place.
  useEffect(() => {
    const offWiki = onWikiChange(() => {
      if (isWikiOpen()) setMenu(null);
    });
    const offClips = onClipsChange(() => {
      if (isClipsOpen()) clipsOpened();
    });
    return () => {
      offWiki();
      offClips();
    };
  }, []);

  // A written reply grows the garland a little per new word. In voice mode,
  // and on a screen that says every answer, huvu's voice grows it instead.
  // A watched feed or a notice is no reply,
  // so the reply under it keeps growing it.
  useEffect(() => {
    const turn = allTurns.findLast(thread.asked);
    if (!turn || turn.at < sessionStart || mode !== "text" || ui.speaksAnswers) return;
    const w = wrote.current.id === turn.id ? wrote.current : { id: turn.id, words: 0, sent: 0 };
    const words = turn.answer.split(/\s+/).filter(Boolean).length;
    const grow = Math.max(0, Math.min(words - w.words, WRITE_WORDS_MAX - w.sent));
    if (grow > 0) growForWords(grow);
    wrote.current = { id: turn.id, words, sent: w.sent + grow };
  }, [allTurns, mode, sessionStart]);

  const sent = useRef(0);
  useEffect(() => {
    if (!outbox || outbox.n === sent.current) return;
    sent.current = outbox.n;
    thread.ask(outbox.text, inputMode);
  }, [outbox, inputMode]);

  // With a keyboard, the text field is always the one in focus while in
  // text mode, so typing and holding space both work without a click
  // first. A touch screen is left alone, since focusing there pops up its
  // keyboard.
  useEffect(() => {
    if (ui.keyboard && mode === "text") textareaRef.current?.focus({ preventScroll: true });
  }, [mode, view]);

  useEffect(() => {
    void refreshFeeds();
  }, []);

  // A deep link with ?mode=voice starts listening at once.
  const startInitialVoice = useEffectEvent(() => {
    if (mode === "voice") voice.begin();
  });
  useEffect(() => startInitialVoice(), []);

  // "hey huvu" (Android only) does what a tap on the flower does.
  const heardWakeWord = useEffectEvent(() => {
    setDocked(false);
    if (voice.phase === "listening") return;
    textareaRef.current?.blur();
    setMode("voice");
    voice.begin();
  });
  useEffect(() => onWakeWord(() => heardWakeWord()), []);

  // History opens scrolled to the newest turn.
  useLayoutEffect(() => {
    const list = listRef.current;
    if (list && view.kind === "history") list.scrollTop = list.scrollHeight;
  }, [view.kind]);

  useEffect(() => {
    if (!TV || view.kind !== "history" || !view.cursor) return;
    const el = rowButtons.current.get(view.cursor);
    el?.focus({ preventScroll: true });
    el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [view]);

  const inVoice = mode === "voice";
  // The session speaks from huvu's first written words, but huvu is only
  // heard once a sentence plays. Until then, and in any quiet while it
  // runs its tools, it is still at work, and the flower says so.
  const phase: VoicePhase = voice.phase === "speaking" && !voice.audible ? "thinking" : voice.phase;
  const hint = voice.trouble ?? (phase === "idle" ? ui.idleHint : PHASE_HINT[phase]);
  // Browsing history in voice mode puts the flower away: it shrinks and
  // fades where it is, and the list takes the whole height. Opening a turn
  // (or scrolling back down) brings it back.
  const flowerAway = inVoice && view.kind === "history";
  const voiceStage = inVoice && !flowerAway;
  // Voice mode: the flower sits in the middle and the turn stays above it,
  // or docked at the bottom, composer-sized, under a turn that fills the
  // rest. Text mode: the flower sits in the composer and the turn fills
  // the rest.
  const flowerAt =
    geo &&
    (inVoice
      ? {
          x: geo.w / 2 - FLOWER_BIG / 2,
          y: (docked ? geo.h - DOCK_Y : geo.h / 2) - FLOWER_BIG / 2,
          scale: (docked ? FLOWER_SMALL / FLOWER_BIG : 1) * (flowerAway ? 0.35 : 1),
          opacity: flowerAway ? 0 : 1,
        }
      : { x: geo.slotX - FLOWER_BIG / 2, y: geo.slotY - FLOWER_BIG / 2, scale: FLOWER_SMALL / FLOWER_BIG, opacity: 1 });
  // In voice history, and under a docked flower, the list stops just above
  // the "write" button.
  const listBottom = !geo ? 0 : voiceStage && !docked ? geo.h / 2 + FLOWER_BIG / 2 : (inVoice ? 88 : geo.composerH) + (PHONE ? waitingH : 0);
  const voiceCaption = voiceStage && !docked;
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
              <TurnView
                key={turn.id}
                turn={turn}
                variant={view.kind === "history" ? "row" : "hero"}
                wide={wide}
                answerShown={ui.voiceAnswerText || !voiceStage}
                rewindable={!wake}
                actions={turnActions}
              />
            ))}
            {view.kind === "stage" && heroId === null && !wake && (
              <m.li
                key="empty"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0, transition: { duration: 0.35, ease: EASE } }}
                transition={{ duration: 0.8, ease: EASE }}
                className="flex flex-col gap-[22px] in-data-[target=firetv]:mt-[12dvh]"
              >
                {/* The TV's screen is big enough to set the greeting huge,
                    in full ivory. */}
                <span className="halo font-bodoni text-[34px] italic leading-[1.1] text-ivory/40 [text-wrap:balance] sm:text-[46px] in-data-[target=firetv]:text-[60px] in-data-[target=firetv]:leading-[1.02] in-data-[target=firetv]:tracking-[-0.02em] in-data-[target=firetv]:text-ivory">
                  {greeting.line}
                </span>
                {greeting.example && (
                  <span className="flex items-baseline gap-3">
                    <span className={`${capsClass} text-sindoor`}>say</span>
                    <span className="halo font-display text-[15px] font-medium text-ivory/85">{greeting.example}</span>
                  </span>
                )}
              </m.li>
            )}
          </AnimatePresence>
        </m.ol>
      </LayoutGroup>

      <p aria-live="polite" className="sr-only">
        {announcement}
      </p>

      {ui.hasInput ? <Menu open={menu} onToggle={menuActions.toggle} onClose={menuActions.close} /> : <TvWordmark />}

      <AnimatePresence>{wake && <WakeBar key="wake" wake={wake} onWatch={() => setScreenOpen(true)} />}</AnimatePresence>

      <AnimatePresence>
        {screenOpen && (
          <Suspense key="screen" fallback={<div className="absolute inset-0 z-40 bg-paper" />}>
            <ScreenView onClose={() => setScreenOpen(false)} />
          </Suspense>
        )}
      </AnimatePresence>

      <Feeds open={feedsOpen} onToggle={feedsActions.toggle} onPick={feedsActions.pick} listRef={feedsRef} />

      <AnimatePresence>
        {wikiOpen && (
          <m.div
            key="wiki"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.4, ease: EASE }}
            className="absolute inset-0 z-40"
          >
            <Suspense fallback={<div className="absolute inset-0 bg-paper" />}>
              <WikiView />
            </Suspense>
          </m.div>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {clipsOpen && (
          <m.div
            key="clips"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.4, ease: EASE }}
            className="absolute inset-0 z-40"
          >
            <Suspense fallback={<div className="absolute inset-0 bg-paper" />}>
              <VoiceClipsView />
            </Suspense>
          </m.div>
        )}
      </AnimatePresence>

      <OutOfReach />

      <m.div
        aria-live="polite"
        initial={false}
        animate={{ opacity: voiceCaption ? 1 : 0 }}
        transition={{ duration: 0.6, ease: EASE }}
        inert={!voiceCaption}
        style={{ top: `calc(50% + ${FLOWER_BIG / 2 + 20}px)` }}
        className={`absolute inset-x-0 z-20 flex flex-col items-center ${voiceCaption ? "" : "pointer-events-none"}`}
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

      {canAsk && (
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
          stopping={stopping}
          onStop={composerActions.stop}
          queued={queued}
          onUnqueue={composerActions.unqueue}
        />
      )}

      {PHONE && geo && <Waiting bottom={inVoice ? 88 : geo.composerH} onHeight={setWaitingH} />}

      {/* One flower for the whole app, above everything. Switching modes
          glides it between the composer and the middle of the screen with
          a transform; it never unmounts, so it never blinks. */}
      {canAsk && flowerAt && (
        <m.div
          initial={false}
          animate={flowerAt}
          transition={{ duration: 0.9, ease: EASE, opacity: { duration: 0.5, ease: EASE } }}
          inert={flowerAway}
          style={{ width: FLOWER_BIG, height: FLOWER_BIG }}
          className={`absolute left-0 top-0 z-30 ${flowerAway ? "pointer-events-none" : ""}`}
        >
          <VoiceFlower
            state={inVoice ? PHASE_FLOWER[phase] : "bud"}
            // Working spins it the same way in both modes: thinking in
            // voice, a reply still coming in by the text field.
            spinning={inVoice ? phase === "thinking" : running}
            size={FLOWER_BIG}
            level={voice.level}
            label={docked ? "bring the flower back" : phase === "thinking" || phase === "speaking" ? "interrupt huvu" : "speak to huvu"}
            onClick={onFlower}
          />
        </m.div>
      )}
    </div>
  );
}

// Over a wake on the stage: what it is for, whether it waits for the user,
// and the way back to the device's own conversation. The browser of a wake
// on pro or advanced can be watched live. Ending it stops what huvu is
// doing there.
function WakeBar({ wake, onWatch }: { wake: WakeInfo; onWatch: () => void }) {
  const [trouble, setTrouble] = useState<string | null>(null);
  const end = () => {
    setTrouble(null);
    endWake(wake.id).catch(() => setTrouble("couldn't end it"));
  };
  const open = wake.status !== "ended" || wake.answerable;
  const status =
    trouble ??
    (wake.status === "waiting" ? `waits for ${wake.waits_for ?? "you"}` : wake.status === "ended" ? (wake.answerable ? "done, reply to carry on" : "ended") : "at work");
  return (
    <m.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.5, ease: EASE }}
      className="absolute inset-x-0 top-16 z-20 mx-auto flex w-full max-w-[52rem] flex-col gap-1 px-6 sm:top-20 sm:px-12"
    >
      <div className="flex items-baseline gap-4">
        <span className={`${capsClass} halo shrink-0 ${wake.status === "waiting" || trouble ? "text-sindoor" : "text-ivory/55"}`}>{status}</span>
        <button
          type="button"
          onClick={leaveWake}
          className={`${capsClass} -my-3 ml-auto min-h-11 shrink-0 px-2 text-ivory/70 outline-none transition-colors duration-500 hover:text-ivory focus-visible:ring-1 focus-visible:ring-sindoor/60`}
        >
          back
        </button>
        {/* Only a wake on pro or advanced may use the browser. */}
        {(wake.tier === "pro" || wake.tier === "advanced") && open && (
          <button
            type="button"
            onClick={onWatch}
            className={`${capsClass} -my-3 min-h-11 shrink-0 px-2 text-ivory/70 outline-none transition-colors duration-500 hover:text-ivory focus-visible:ring-1 focus-visible:ring-sindoor/60`}
          >
            watch
          </button>
        )}
        {open && (
          <button
            type="button"
            onClick={end}
            className={`${capsClass} -my-3 min-h-11 shrink-0 px-2 text-ivory/70 outline-none transition-colors duration-500 hover:text-sindoor focus-visible:ring-1 focus-visible:ring-sindoor/60`}
          >
            end
          </button>
        )}
      </div>
      <p className="halo m-0 truncate font-display text-[13px] font-medium text-ivory/60">{wake.note}</p>
    </m.div>
  );
}

// Says so while huvu can't be reached, between the wordmark and the
// feeds. What was asked meanwhile says it failed in its own turn.
function OutOfReach() {
  const reach = useReach();
  return (
    <AnimatePresence initial={false}>
      {reach === "down" && (
        <m.span
          role="status"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.6, ease: EASE }}
          className={`${capsClass} halo pointer-events-none absolute inset-x-0 z-20 flex justify-center text-sindoor ${
            TV ? "top-7 sm:top-10" : "top-3.5 min-h-11 items-center sm:top-[26px]"
          }`}
        >
          can't reach huvu
        </m.span>
      )}
    </AnimatePresence>
  );
}

// The TV's wordmark, with who is recognized at it: only someone who said
// yes on their phone, so the name is never a guess. While it waits for
// that yes, it says whose phone to look at.
function TvWordmark() {
  const { here, asking } = useWaiting();
  return (
    <span className="halo pointer-events-none absolute left-6 top-7 z-20 flex items-baseline gap-5 sm:left-12 sm:top-10">
      <span className={`${capsClass} text-[11px] tracking-[0.42em] text-ivory/75`}>huvu</span>
      <AnimatePresence mode="wait" initial={false}>
        {(here || asking) && (
          <m.span
            key={here ? `here-${here.id}` : `asking-${asking?.person.id}`}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.6, ease: EASE }}
            className={`${capsClass} ${here ? "text-sindoor" : "text-ivory/55"}`}
          >
            {here ? `with ${here.name}` : `${asking?.person.name}, check your phone`}
          </m.span>
        )}
      </AnimatePresence>
    </span>
  );
}
