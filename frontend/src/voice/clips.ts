import { useSyncExternalStore } from "react";
import { asked, getThread, subscribe } from "../chat/thread";
import { call, errorText, getJson } from "../net/http";
import { ClipRecorder, wav, type Utterance } from "./clipRecorder";
import { micTrouble } from "./mic";
import { STEPS, type Step } from "./clipSteps";

// Recording a person's voice for the "hey huvu" model: the session behind
// the recording screen (components/VoiceClips.tsx). It walks the steps in
// clipSteps.ts, keeps each clip that passes (backend/src/huvu/voice_clips.py),
// and talks the person through it in huvu's voice: the step, a chime for
// each clip kept, and why one wasn't. It picks up where the person left
// off, from what the backend already has.

export type Phase = "loading" | "ready" | "running" | "paused" | "done";

export interface Note {
  text: string;
  kept: boolean;
}

export interface ClipState {
  phase: Phase;
  // The step now, an index into STEPS.
  step: number;
  // Clips kept so far, by step id. For talk, seconds.
  counts: Record<string, number>;
  note: Note | null;
  // Why it can't record: no mic, or huvu isn't answering.
  trouble: string | null;
  // Whether the room's noise is measured, so it listens.
  listening: boolean;
  // A clip kept in this session can be dropped again.
  canUndo: boolean;
}

type Verdict = "kept" | "too_loud" | "too_quiet" | "too_long";

const NOTES: Record<Exclude<Verdict, "kept">, string> = {
  too_loud: "Too loud: it clipped. Step back a little.",
  too_quiet: "That was hard to hear over the room. A little louder.",
  too_long: "That was long. Say only what the screen shows.",
};

const TALK_PIECE_SECONDS = 15;

export function amount(step: Step, counts: Record<string, number>): number {
  return Math.min(counts[step.id] ?? 0, step.target);
}

export function promptOf(step: Step, counts: Record<string, number>): string | null {
  return step.prompts ? step.prompts[(counts[step.id] ?? 0) % step.prompts.length] : null;
}

function judge(u: Utterance, step: Step): Verdict {
  const longest = step.kind === "wake" ? 2200 : 3500;
  if (u.peak >= 0.99) return "too_loud";
  if (u.speechMs > longest) return "too_long";
  if (u.snr < (step.far ? 2 : 4)) return "too_quiet";
  return "kept";
}

function playCue(name: string): Promise<void> {
  return new Promise((resolve) => {
    const audio = new Audio(`${import.meta.env.BASE_URL}audio/cues/${name}.mp3`);
    audio.onended = () => resolve();
    audio.onerror = () => resolve();
    audio.play().catch(() => resolve());
  });
}

class ClipSession {
  state: ClipState = { phase: "loading", step: 0, counts: {}, note: null, trouble: null, listening: false, canUndo: false };
  private listeners = new Set<() => void>();
  private levelListeners = new Set<(rms: number, floor: number) => void>();
  private recorder: ClipRecorder | null = null;
  // Kept in this session, newest last, for "that wasn't it".
  private kept: { id: string; step: string }[] = [];
  // Each clip is judged, kept and answered in turn.
  private queue = Promise.resolve();

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  onLevel(listener: (rms: number, floor: number) => void): () => void {
    this.levelListeners.add(listener);
    return () => this.levelListeners.delete(listener);
  }

  private set(next: Partial<ClipState>): void {
    this.state = { ...this.state, ...next };
    for (const l of this.listeners) l();
  }

  async load(): Promise<void> {
    this.set({ phase: "loading", trouble: null, note: null });
    try {
      const { steps } = await getJson<{ steps: Record<string, number> }>("/api/voice-clips");
      const first = STEPS.findIndex((s) => amount(s, steps) < s.target);
      this.set({ phase: first < 0 ? "done" : "ready", step: Math.max(first, 0), counts: steps });
    } catch (err) {
      this.set({ phase: "ready", trouble: errorText(err) });
    }
  }

  // From a tap: the mic and the cues need a gesture.
  async start(step: number): Promise<void> {
    this.stop();
    this.set({ step, trouble: null, note: null, listening: false, canUndo: false });
    this.kept = [];
    try {
      this.recorder = await ClipRecorder.open({
        level: (rms, floor) => {
          for (const l of this.levelListeners) l(rms, floor);
          if (!this.state.listening && this.recorder?.ready) this.set({ listening: true });
        },
        utterance: (u) => this.enqueue(() => this.heard(u)),
        piece: (samples) => this.enqueue(() => this.talked(samples)),
      });
    } catch (err) {
      this.set({ phase: "ready", trouble: micTrouble(err) });
      return;
    }
    this.set({ phase: "running" });
    await this.introduce();
  }

  pause(): void {
    if (this.state.phase !== "running") return;
    this.stop();
    this.set({ phase: "paused", listening: false });
  }

  close(): void {
    this.stop();
    this.set({ phase: this.state.phase === "done" ? "done" : "ready", listening: false, note: null });
  }

  skip(): void {
    if (this.state.phase === "running") this.enqueue(() => this.advance());
  }

  undo(): void {
    this.enqueue(async () => {
      const last = this.kept.pop();
      if (!last) return;
      try {
        await call(`/api/voice-clips/${last.id}`, { method: "DELETE" });
        this.set({ counts: { ...this.state.counts, [last.step]: Math.max(0, (this.state.counts[last.step] ?? 0) - 1) }, note: { text: "Dropped the last one.", kept: false } });
      } catch (err) {
        this.kept.push(last);
        this.set({ note: { text: errorText(err), kept: false } });
      }
      this.set({ canUndo: this.kept.length > 0 });
    });
  }

  private stop(): void {
    this.recorder?.close();
    this.recorder = null;
    this.queue = Promise.resolve();
  }

  private enqueue(job: () => Promise<void>): void {
    const recorder = this.recorder;
    this.queue = this.queue.then(() => (this.recorder === recorder ? job() : undefined)).catch((err: unknown) => console.error("voice clips:", err));
  }

  private get current(): Step {
    return STEPS[this.state.step];
  }

  private say(name: string): Promise<void> {
    return this.recorder ? this.recorder.deafWhile(playCue(name)) : Promise.resolve();
  }

  // The step's cue, then its first prompt where huvu says them.
  private async introduce(): Promise<void> {
    const step = this.current;
    this.recorder?.setTalk(!!step.talk);
    this.recorder?.setFar(!!step.far);
    await this.say(step.id);
    await this.sayPrompt();
  }

  private async sayPrompt(): Promise<void> {
    const step = this.current;
    if (step.sayPrompts && step.prompts) await this.say(`${step.id}_${(this.state.counts[step.id] ?? 0) % step.prompts.length}`);
  }

  private async heard(u: Utterance): Promise<void> {
    const step = this.current;
    if (step.talk) return;
    const verdict = judge(u, step);
    if (verdict !== "kept") {
      this.set({ note: { text: NOTES[verdict], kept: false } });
      await this.say(verdict === "too_long" && step.kind === "other" ? "too_long_line" : verdict);
      return;
    }
    if (!(await this.keep(step, wav(u.samples)))) return;
    await this.recorder?.chime();
    const left = step.target - amount(step, this.state.counts);
    if (left === 0) {
      await this.advance();
      return;
    }
    if (left === 5) await this.say("five_more");
    await this.sayPrompt();
  }

  private async talked(samples: Float32Array): Promise<void> {
    const step = this.current;
    if (!step.talk || !(await this.keep(step, wav(samples), TALK_PIECE_SECONDS))) return;
    if (amount(step, this.state.counts) >= step.target) await this.advance();
  }

  // Sends a clip, and counts it once the backend has it.
  private async keep(step: Step, clip: Blob, worth = 1): Promise<boolean> {
    try {
      const res = await call(`/api/voice-clips/${step.kind}/${step.id}`, { method: "POST", body: clip, headers: { "Content-Type": "audio/wav" } });
      const { id } = (await res.json()) as { id: string };
      if (!step.talk) this.kept.push({ id, step: step.id });
      this.set({ counts: { ...this.state.counts, [step.id]: (this.state.counts[step.id] ?? 0) + worth }, note: { text: "Kept.", kept: true }, canUndo: this.kept.length > 0 });
      return true;
    } catch (err) {
      this.set({ note: { text: errorText(err), kept: false } });
      await this.say("try_again");
      return false;
    }
  }

  private async advance(): Promise<void> {
    const next = STEPS.findIndex((s, i) => i > this.state.step && amount(s, this.state.counts) < s.target);
    if (next < 0) {
      this.recorder?.setTalk(false);
      await this.say("done");
      this.stop();
      this.set({ phase: "done", listening: false, note: null, canUndo: false });
      return;
    }
    this.kept = [];
    this.set({ step: next, note: null, canUndo: false });
    await this.introduce();
  }
}

export const clips = new ClipSession();

export function useClips(): ClipState {
  return useSyncExternalStore(
    (l) => clips.subscribe(l),
    () => clips.state,
  );
}

// Whether the recording screen is open over the stage. The menu opens it,
// and the stage's back closes it.
let open = false;
const openListeners = new Set<() => void>();

export function setClipsOpen(next: boolean): void {
  if (open === next) return;
  open = next;
  if (!next) clips.close();
  for (const l of openListeners) l();
}

export function onClipsChange(listener: () => void): () => void {
  openListeners.add(listener);
  return () => openListeners.delete(listener);
}

export function useClipsOpen(): boolean {
  return useSyncExternalStore(onClipsChange, () => open);
}

export function isClipsOpen(): boolean {
  return open;
}

// huvu opens the screen with ui_learn_voice: once, as its part arrives in
// a running turn. The same part on a turn read back later opens nothing.
const opened = new Set<string>();
subscribe(() => {
  for (const turn of getThread().turns.slice(-3)) {
    if (!turn.running || !asked(turn)) continue;
    for (const part of turn.parts) {
      if (part.kind !== "tool" || part.toolName !== "learn_voice" || opened.has(part.id)) continue;
      opened.add(part.id);
      setClipsOpen(true);
    }
  }
});
