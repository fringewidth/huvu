import { call } from "../net/http";
import { audioContext, rms } from "./audio";
import { acquireMic, type Mic } from "./mic";

// huvu's voice: the backend says a turn's answer while huvu writes it
// (GET /api/turns/{id}/speech, Edge's voices on the backend), a sentence at a
// time, as raw 16-bit mono PCM. Each piece is scheduled right after the one
// before, so the sentences play without gaps. It plays through an analyser
// so the garden can grow the garland with it, and while it plays, the mic
// listens for the user talking over it.
const SAMPLE_RATE = 24000;
// Mic loudness that counts as talking over huvu.
const BARGE_IN_RMS = 0.15;
// Audio gathered before it is scheduled: tiny network pieces would make
// hundreds of buffers.
const MIN_SAMPLES = SAMPLE_RATE / 10;
// How far ahead the first piece starts, so it never starts in the past.
const LEAD = 0.05;

let out: { analyser: AnalyserNode; buf: Uint8Array<ArrayBuffer> } | null = null;
let current: { finish: () => void } | null = null;
const startListeners = new Set<() => void>();

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

export function stopSpeaking(): void {
  current?.finish();
}

// Says turn `turnId`'s answer. Resolves once huvu is done: the last
// sentence ended, the user talked over it, or there is no voice.
export async function speak(turnId: string): Promise<void> {
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
      if (current?.finish === finish) current = null;
      resolve();
    };
    current = { finish };

    const play = (samples: Float32Array<ArrayBuffer>) => {
      const buffer = ctx.createBuffer(1, samples.length, SAMPLE_RATE);
      buffer.copyToChannel(samples, 0);
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(output().analyser);
      // A sentence that came late starts now, not in the past.
      nextAt = Math.max(nextAt, ctx.currentTime + LEAD);
      source.start(nextAt);
      nextAt += buffer.duration;
      sources.add(source);
      source.onended = () => {
        sources.delete(source);
        if (streamed && sources.size === 0) finish();
      };
      if (!started) {
        started = true;
        for (const listener of startListeners) listener();
        listenForBargeIn();
      }
    };

    const listenForBargeIn = () => {
      const watch = () => {
        if (done || !mic) return;
        if (mic.rms() > BARGE_IN_RMS) return finish();
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
        // Bytes not yet played: an odd byte waits for its pair.
        let pending = new Uint8Array(0);
        const flush = (all: boolean) => {
          const usable = pending.length - (pending.length % 2);
          if (usable === 0 || (!all && usable / 2 < MIN_SAMPLES)) return;
          const view = new DataView(pending.buffer, pending.byteOffset, usable);
          const samples = new Float32Array(usable / 2);
          for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
          pending = pending.slice(usable);
          play(samples);
        };
        while (!done) {
          const { done: ended, value } = await reader.read();
          if (ended) break;
          const joined = new Uint8Array(pending.length + value.length);
          joined.set(pending);
          joined.set(value, pending.length);
          pending = joined;
          flush(false);
        }
        flush(true);
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
