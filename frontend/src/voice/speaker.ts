import { audioContext, rms } from "./audio";
import { acquireMic, type Mic } from "./mic";

// huvu's voice. For now it is one bundled clip (made with macOS `say`),
// until real TTS arrives through the gateway. It plays through an analyser
// so the garden can grow the garland with it, and while it plays, the mic
// listens for the user talking over it.
const TEST_VOICE_URL = "/audio/test-voice.m4a";
// Mic loudness that counts as talking over huvu.
const BARGE_IN_RMS = 0.15;

let clip: Promise<AudioBuffer> | null = null;
let out: { analyser: AnalyserNode; buf: Uint8Array<ArrayBuffer> } | null = null;
let current: { source: AudioBufferSourceNode; finish: () => void } | null = null;
const startListeners = new Set<() => void>();

// Fetches and decodes the clip ahead of time, so a reply starts talking
// the moment its first words arrive.
export function warmSpeaker(): Promise<AudioBuffer> {
  clip ??= fetch(TEST_VOICE_URL)
    .then((res) => res.arrayBuffer())
    .then((data) => audioContext().decodeAudioData(data));
  clip.catch(() => (clip = null));
  return clip;
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

// Resolves once huvu is done: the clip ended, or the user talked over it.
export async function speak(): Promise<void> {
  stopSpeaking();
  const ctx = audioContext();
  const [buffer] = await Promise.all([warmSpeaker(), ctx.resume()]);
  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(output().analyser);

  return new Promise((resolve) => {
    let mic: Mic | null = null;
    let raf = 0;
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      cancelAnimationFrame(raf);
      mic?.release();
      source.onended = null;
      source.stop();
      source.disconnect();
      if (current?.source === source) current = null;
      resolve();
    };
    current = { source, finish };
    source.onended = finish;
    source.start();
    for (const listener of startListeners) listener();

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
