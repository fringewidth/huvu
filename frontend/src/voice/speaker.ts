import { call } from "../net/http";
import { audioContext, rms } from "./audio";
import { acquireMic, type Mic } from "./mic";

// huvu's voice: the backend says a turn's answer while huvu writes it
// (GET /api/turns/{id}/speech, Edge's voices on the backend), one MP3 clip
// per sentence, each framed by its length as four big-endian bytes. Clips
// are decoded in order and each is scheduled right after the one before,
// so the sentences play without gaps. It plays through an analyser so the
// garden can grow the garland with it. Hands-free, the mic listens while
// it plays for the user talking over it; push-to-talk has no such thing,
// since holding the key is how to cut huvu off there. Only while it
// plays: between sentences,
// while huvu runs its tools, there is nothing to talk over, and the mic's
// gain climbs in the quiet until room noise would cut the rest off.
// Mic loudness that counts as talking over huvu.
const BARGE_IN_RMS = 0.15;
// How far ahead the first piece starts, so it never starts in the past.
const LEAD = 0.05;

let out: { analyser: AnalyserNode; buf: Uint8Array<ArrayBuffer> } | null = null;
let current: { finish: () => void } | null = null;
const startListeners = new Set<() => void>();
// Whether a sentence is playing or waits its turn to play. Between
// sentences, and while huvu runs its tools, it is false.
let audible = false;
const audibleListeners = new Set<(audible: boolean) => void>();

function setAudible(next: boolean): void {
  if (next === audible) return;
  audible = next;
  for (const listener of audibleListeners) listener(next);
}

// Nothing to fetch ahead of time any more; the audio context only has to
// be running before the first sentence arrives.
export async function warmSpeaker(): Promise<void> {
  await audioContext().resume();
}

function output(): { analyser: AnalyserNode; buf: Uint8Array<ArrayBuffer> } {
  if (!out) {
    const ctx = audioContext();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    analyser.smoothingTimeConstant = 0.3;
    analyser.connect(ctx.destination);
    out = { analyser, buf: new Uint8Array(analyser.fftSize) };
  }
  return out;
}

export function onSpeakingStart(listener: () => void): () => void {
  startListeners.add(listener);
  return () => startListeners.delete(listener);
}

export function onAudible(listener: (audible: boolean) => void): () => void {
  audibleListeners.add(listener);
  return () => audibleListeners.delete(listener);
}

export function stopSpeaking(): void {
  current?.finish();
}

// Whether huvu is saying an answer, or about to: from speak() until it
// resolves.
export function speaking(): boolean {
  return current !== null;
}

// Says turn `turnId`'s answer. Resolves once huvu is done: the last
// sentence ended, the user talked over it (with `bargeIn`), or there is no
// voice.
export async function speak(turnId: string, bargeIn: boolean): Promise<void> {
  stopSpeaking();
  const ctx = audioContext();
  await ctx.resume();
  const abort = new AbortController();
  const sources = new Set<AudioBufferSourceNode>();

  return new Promise((resolve) => {
    let mic: Mic | null = null;
    let raf = 0;
    let done = false;
    let streamed = false;
    let started = false;
    let nextAt = 0;
    const finish = () => {
      if (done) return;
      done = true;
      cancelAnimationFrame(raf);
      mic?.release();
      abort.abort();
      for (const source of sources) {
        source.onended = null;
        source.stop();
        source.disconnect();
      }
      sources.clear();
      setAudible(false);
      if (current?.finish === finish) current = null;
      resolve();
    };
    current = { finish };

    const play = (buffer: AudioBuffer) => {
      if (done) return;
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(output().analyser);
      // A sentence that came late starts now, not in the past.
      nextAt = Math.max(nextAt, ctx.currentTime + LEAD);
      source.start(nextAt);
      nextAt += buffer.duration;
      sources.add(source);
      setAudible(true);
      source.onended = () => {
        sources.delete(source);
        if (sources.size === 0) setAudible(false);
        if (streamed && sources.size === 0) finish();
      };
      if (!started) {
        started = true;
        for (const listener of startListeners) listener();
        if (bargeIn) listenForBargeIn();
      }
    };

    const listenForBargeIn = () => {
      const watch = () => {
        if (done || !mic) return;
        if (sources.size > 0 && mic.rms() > BARGE_IN_RMS) return finish();
        raf = requestAnimationFrame(watch);
      };
      acquireMic()
        .then((m) => {
          if (done) return m.release();
          mic = m;
          watch();
        })
        .catch((err) => console.error("speaker: no mic for barge-in", err));
    };

    void (async () => {
      try {
        const res = await call(`/api/turns/${turnId}/speech`, { signal: abort.signal });
        if (!res.body) throw new Error("speaker: the backend sent no audio");
        const reader = res.body.getReader();
        // Bytes of clips not yet whole, and the decodes in order.
        let pending = new Uint8Array(0);
        let decoded: Promise<void> = Promise.resolve();
        const takeClips = () => {
          while (pending.length >= 4) {
            const length = new DataView(pending.buffer, pending.byteOffset, 4).getUint32(0);
            if (pending.length < 4 + length) return;
            const clip = pending.slice(4, 4 + length).buffer;
            pending = pending.slice(4 + length);
            decoded = decoded.then(() => ctx.decodeAudioData(clip)).then(play, (err) => console.error("speaker: a clip didn't decode", err));
          }
        };
        while (!done) {
          const { done: ended, value } = await reader.read();
          if (ended) break;
          const joined = new Uint8Array(pending.length + value.length);
          joined.set(pending);
          joined.set(value, pending.length);
          pending = joined;
          takeClips();
        }
        await decoded;
        streamed = true;
        if (sources.size === 0) finish();
      } catch (err) {
        if (abort.signal.aborted) return;
        console.error("speaker: playback failed", err);
        finish();
      }
    })();
  });
}

let fast = 0;
let slow = 0;
let peak = 0.05;
let loud = 0;
let lastOnset = -1;

// Read by the garden every frame. `onset` marks the start of a syllable.
export function sampleVoice(dt: number, now: number): { active: boolean; loud: number; onset: boolean } {
  if (!current || !out) {
    loud *= Math.max(0, 1 - dt * 6);
    return { active: false, loud, onset: false };
  }
  const level = rms(out.analyser, out.buf);
  peak = Math.max(peak * (1 - dt * 0.08), level, 0.04);
  loud = Math.min(1, level / peak);
  fast += (level - fast) * Math.min(1, dt * 30);
  slow += (level - slow) * Math.min(1, dt * 4);
  const onset = fast > slow * 1.35 + 0.012 && loud > 0.18 && now - lastOnset > 0.13;
  if (onset) lastOnset = now;
  return { active: true, loud, onset };
}
