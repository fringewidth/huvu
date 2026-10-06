import { useEffect, useEffectEvent, useLayoutEffect, useRef, useState } from "react";
import { useMotionValue } from "motion/react";
import { asked, getThread, stop as stopThread, subscribe } from "../chat/thread";
import { resumeAudio } from "./audio";
import { acquireMic, closeMic, type Mic } from "./mic";
import { speak, stopSpeaking, warmSpeaker } from "./speaker";
import { createTranscriber } from "./transcriber";

// One turn of voice mode goes listening, then thinking once the utterance
// is sent, then speaking once huvu's reply starts, then back. Hands-free
// (entered from the flower) listens again by itself after huvu speaks.
// Push-to-talk (space on web) waits in idle for the
// next hold. Either way in cuts huvu off first, voice and text both.
export type VoicePhase = "idle" | "listening" | "thinking" | "speaking";

// Silent rounds hands-free tolerates before it stops listening, so a quiet
// room doesn't loop forever.
const MAX_SILENT_ROUNDS = 3;
// Share of the recent peak that counts as room noise, not speech.
const NOISE_FLOOR = 0.12;

export function useVoiceSession(onUtterance: (text: string) => void) {
  const [phase, setPhase] = useState<VoicePhase>("idle");
  const [transcript, setTranscript] = useState("");
  const [transcriber] = useState(createTranscriber);
  const level = useMotionValue(0);
  const handsFree = useRef(false);
  const silentRounds = useRef(0);
  // Where the sent utterance's reply stands. "sent" until its turn shows
  // up running: before that, the last answer is still the previous turn's
  // and must not count.
  const awaiting = useRef<"none" | "sent" | "running">("none");
  // Only the latest reply's end moves the session on.
  const utterance = useRef(0);
  const utter = useRef(onUtterance);
  useLayoutEffect(() => {
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

  // Every way into voice is a user gesture. Wake the audio here, and load
  // huvu's voice before there is anything to say.
  function warm(): void {
    resumeAudio();
    void warmSpeaker().catch((err) => console.error("voice: clip failed to load", err));
  }

  // Drops the reply in flight: huvu is asked to stop and its voice goes
  // quiet. Words Hermes already sent still arrive. The utterance count
  // moves on, so the cut-off clip's end can't move the session on again.
  function interrupt(): void {
    awaiting.current = "none";
    utterance.current++;
    stopThread();
    stopSpeaking();
  }

  // Hands-free, from the flower.
  function begin(): void {
    warm();
    interrupt();
    handsFree.current = true;
    silentRounds.current = 0;
    listen(false);
  }

  // Push-to-talk.
  function hold(): void {
    warm();
    interrupt();
    handsFree.current = false;
    listen(true);
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
    closeMic();
  }

  const onSpoke = useEffectEvent((id: number) => {
    if (id === utterance.current && phase === "speaking") afterSpeech();
  });

  const onThread = useEffectEvent(() => {
    if (awaiting.current === "none") return;
    const { turns, running } = getThread();
    if (running) awaiting.current = "running";
    if (awaiting.current !== "running") return;
    // A feed opened or a notice sent meanwhile sits on top, and is no
    // answer.
    if (turns.findLast(asked)?.answer) {
      awaiting.current = "none";
      setPhase("speaking");
      const id = ++utterance.current;
      speak()
        .catch((err) => console.error("voice: playback failed", err))
        .finally(() => onSpoke(id));
      return;
    }
    // The run ended with nothing said (backend down, cancelled).
    if (!running) {
      awaiting.current = "none";
      afterSpeech();
    }
  });

  useEffect(() => subscribe(() => onThread()), []);

  // The flower swells with your voice while it listens.
  useEffect(() => {
    if (phase !== "listening") return;
    let mic: Mic | null = null;
    let raf = 0;
    let dead = false;
    acquireMic()
      .then((m) => {
        if (dead) return m.release();
        mic = m;
        let peak = 0.05;
        let smooth = 0;
        const tick = () => {
          const r = m.rms();
          peak = Math.max(peak * 0.995, r, 0.04);
          const raw = Math.max(0, Math.min(1, (r / peak - NOISE_FLOOR) / (1 - NOISE_FLOOR)));
          // Rise a little faster than fall, and never jump: the raw level
          // twitches with every syllable.
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
      mic?.release();
      level.set(0);
    };
  }, [phase, level]);

  useEffect(
    () => () => {
      transcriber.abort();
      stopSpeaking();
    },
    [transcriber],
  );

  return { phase, transcript, level, begin, hold, dropHold, release, exit };
}
