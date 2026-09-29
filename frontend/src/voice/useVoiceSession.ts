import { useEffect, useEffectEvent, useRef, useState } from "react";
import type { AssistantRuntime } from "@assistant-ui/react";
import { useMotionValue } from "motion/react";
import { HUVU_SPEAK_EVENT, HUVU_SPOKE_EVENT } from "../canvas/events";
import { startMicMeter, type MicMeter } from "./micMeter";
import { createTranscriber } from "./transcriber";

// One turn of voice mode goes listening, then thinking once the utterance
// is sent, then speaking once huvu's reply starts, then back. Hands-free
// (entered from the flower) listens again by itself after huvu speaks.
// Push-to-talk (space on web, select on Fire TV) waits in idle for the
// next hold.
export type VoicePhase = "idle" | "listening" | "thinking" | "speaking";

// Silent rounds hands-free tolerates before it stops listening, so a quiet
// room doesn't loop forever.
const MAX_SILENT_ROUNDS = 3;

export function useVoiceSession(runtime: AssistantRuntime, onUtterance: (text: string) => void) {
  const [phase, setPhase] = useState<VoicePhase>("idle");
  const [transcript, setTranscript] = useState("");
  const [transcriber] = useState(createTranscriber);
  const level = useMotionValue(0);
  const handsFree = useRef(false);
  const silentRounds = useRef(0);
  // Where the sent utterance's reply stands. "sent" until its run shows
  // up in the thread: before that, the thread's last answer is still the
  // previous turn's and must not count.
  const awaiting = useRef<"none" | "sent" | "running">("none");
  const utter = useRef(onUtterance);

  useEffect(() => {
    utter.current = onUtterance;
  });

  function listen(hold: boolean): void {
    setTranscript("");
    setPhase("listening");
    transcriber.start({
      hold,
      onText: setTranscript,
      onEnd: (text) => {
        if (text) {
          silentRounds.current = 0;
          awaiting.current = "sent";
          setPhase("thinking");
          // Runs before the transcript clears, so the caller can still
          // measure it on screen and fly it up into the YOU cue.
          utter.current(text);
          setTranscript("");
          return;
        }
        setTranscript("");
        silentRounds.current++;
        if (handsFree.current && silentRounds.current < MAX_SILENT_ROUNDS) listen(false);
        else setPhase("idle");
      },
    });
  }

  function afterSpeech(): void {
    if (handsFree.current) listen(false);
    else setPhase("idle");
  }

  function begin(free: boolean): void {
    handsFree.current = free;
    silentRounds.current = 0;
    if (free) listen(false);
    else setPhase("idle");
  }

  // Push-to-talk. Returns whether listening started: not while huvu is
  // still working on the last utterance.
  function hold(): boolean {
    if (awaiting.current !== "none") return false;
    handsFree.current = false;
    listen(true);
    return true;
  }

  // A push-to-talk tap too short to mean anything: drop what was heard.
  function dropHold(): void {
    transcriber.abort();
    setTranscript("");
    setPhase("idle");
  }

  function release(): void {
    transcriber.stop();
  }

  function exit(): void {
    handsFree.current = false;
    awaiting.current = "none";
    transcriber.abort();
    setTranscript("");
    setPhase("idle");
  }

  const onThread = useEffectEvent(() => {
    if (awaiting.current === "none") return;
    const { messages, isRunning } = runtime.thread.getState();
    if (isRunning) awaiting.current = "running";
    if (awaiting.current !== "running") return;
    const last = messages.at(-1);
    const said = last?.role === "assistant" && last.content.some((p) => p.type === "text" && p.text.trim() !== "");
    if (said) {
      awaiting.current = "none";
      window.dispatchEvent(new CustomEvent(HUVU_SPEAK_EVENT));
      setPhase("speaking");
      return;
    }
    // The run ended with nothing said (backend down, cancelled).
    if (!isRunning) {
      awaiting.current = "none";
      afterSpeech();
    }
  });

  const onSpoke = useEffectEvent(() => {
    if (phase === "speaking") afterSpeech();
  });

  useEffect(() => runtime.thread.subscribe(() => onThread()), [runtime]);

  useEffect(() => {
    const handler = () => onSpoke();
    window.addEventListener(HUVU_SPOKE_EVENT, handler);
    return () => window.removeEventListener(HUVU_SPOKE_EVENT, handler);
  }, []);

  useEffect(() => {
    if (phase !== "listening") return;
    let meter: MicMeter | null = null;
    let raf = 0;
    let dead = false;
    startMicMeter()
      .then((m) => {
        if (dead) return m.stop();
        meter = m;
        // Rise a little faster than fall, and never jump: the raw level
        // twitches with every syllable.
        let smooth = 0;
        const tick = () => {
          const raw = m.level();
          smooth += (raw - smooth) * (raw > smooth ? 0.12 : 0.05);
          level.set(smooth);
          raf = requestAnimationFrame(tick);
        };
        tick();
      })
      .catch((err) => console.error("voice: mic meter failed", err));
    return () => {
      dead = true;
      cancelAnimationFrame(raf);
      meter?.stop();
      level.set(0);
    };
  }, [phase, level]);

  useEffect(() => () => transcriber.abort(), [transcriber]);

  return { phase, transcript, level, begin, hold, dropHold, release, exit };
}
