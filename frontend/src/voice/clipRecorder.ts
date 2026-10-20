import { holdWakeMic } from "./wakeWord";

// The recording screen's ears (components/VoiceClips.tsx). It records the
// mic raw, the way the wake word hears it on the phone: 16 kHz, with the
// browser's echo cancelling, noise suppression and gain control off. It
// learns the room's noise first, then cuts out each thing said, from a
// little before it starts to a little after a pause ends it. For ordinary
// talk it cuts the take into pieces of 15 s instead.
//
// It has its own AudioContext at 16 kHz, apart from the app's (audio.ts),
// since the clips must be at the rate the model hears.

export const RATE = 16000;
const FRAME = 320; // 20 ms
// Room noise is measured over the first frames, before anything counts.
const CALIBRATE_FRAMES = 40;
// Kept before the sound that starts an utterance, and after it ends.
const PAD_FRAMES = 15;
const START_FRAMES = 3;
// The pause that ends an utterance. "hey" and "huvu" have a short gap
// between them, well under this. A sentence's pauses are shorter too, so
// a sentence comes whole, and is judged too long, not cut into pieces
// that each pass as the phrase.
const END_FRAMES = 40;
// How far over the room's noise sound must rise to start an utterance,
// and the least that starts one, whatever the room. Close to the phone, a
// breath in a quiet room would. Away from it, a voice from the next room
// is about as faint as that breath, and nothing near the phone breathes.
interface Sensitivity {
  over: number;
  start: number;
  end: number;
}
const NEAR: Sensitivity = { over: 3.2, start: 0.005, end: 0.003 };
const FAR: Sensitivity = { over: 2.2, start: 0.0012, end: 0.0008 };
// An utterance longer than this is cut off and judged too long.
const MAX_FRAMES = 200;
// Shorter is a knock or a cough, and is dropped without a word.
const MIN_SPEECH_MS = 250;
const TALK_PIECE = 15 * RATE;
// After a cue or the chime, the room echoes for a moment.
const ECHO_MS = 250;

export interface Utterance {
  samples: Float32Array;
  speechMs: number;
  // The loudest sample, where 1 is the mic's limit.
  peak: number;
  // How far the loudest 20 ms rose over the room's noise.
  snr: number;
}

export interface RecorderEvents {
  // Each frame, for the meter: its loudness and the room's.
  level(rms: number, floor: number): void;
  utterance(u: Utterance): void;
  // Talk mode: the next 15 s of the take.
  piece(samples: Float32Array): void;
}

const WORKLET = `registerProcessor("huvu-clips", class extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0][0];
    if (channel) this.port.postMessage(channel.slice(0));
    return true;
  }
});`;

export class ClipRecorder {
  private frame = new Float32Array(FRAME);
  private filled = 0;
  private calibrated = 0;
  private floor = 0;
  private recent: Float32Array[] = [];
  private speech: Float32Array[] | null = null;
  private loudRun = 0;
  private quietRun = 0;
  private maxRms = 0;
  private peak = 0;
  private deafUntil = 0;
  private deafCount = 0;
  private talk: Float32Array[] | null = null;
  private talkLength = 0;
  // Resampling, where the browser won't run a context at 16 kHz.
  private ratio: number;
  private sensitivity = NEAR;
  private at = 0;
  private prev = 0;

  private ctx: AudioContext;
  private stream: MediaStream;
  private events: RecorderEvents;

  private constructor(ctx: AudioContext, stream: MediaStream, events: RecorderEvents) {
    this.ctx = ctx;
    this.stream = stream;
    this.events = events;
    this.ratio = ctx.sampleRate / RATE;
  }

  static async open(events: RecorderEvents): Promise<ClipRecorder> {
    await holdWakeMic(true);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
    } catch (err) {
      void holdWakeMic(false);
      throw err;
    }
    let ctx: AudioContext;
    try {
      ctx = new AudioContext({ sampleRate: RATE });
    } catch {
      ctx = new AudioContext();
    }
    const recorder = new ClipRecorder(ctx, stream, events);
    const url = URL.createObjectURL(new Blob([WORKLET], { type: "text/javascript" }));
    try {
      await ctx.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
    const node = new AudioWorkletNode(ctx, "huvu-clips");
    node.port.onmessage = (e: MessageEvent<Float32Array>) => recorder.push(e.data);
    // A node no one listens to may not run, so it plays into silence.
    const silence = ctx.createGain();
    silence.gain.value = 0;
    ctx.createMediaStreamSource(stream).connect(node).connect(silence).connect(ctx.destination);
    await ctx.resume();
    return recorder;
  }

  // Whether the room's noise is known yet.
  get ready(): boolean {
    return this.calibrated >= CALIBRATE_FRAMES;
  }

  // Whether the person is away from the phone, so a faint voice counts.
  setFar(far: boolean): void {
    this.sensitivity = far ? FAR : NEAR;
  }

  // Talk mode keeps everything, in pieces. Turning it off drops the piece
  // still filling.
  setTalk(on: boolean): void {
    this.talk = on ? [] : null;
    this.talkLength = 0;
  }

  // Hears nothing while `sound` plays (a cue, the chime), and for the echo
  // after it.
  async deafWhile<T>(sound: Promise<T>): Promise<T> {
    this.deafCount++;
    this.speech = null;
    this.loudRun = 0;
    try {
      return await sound;
    } finally {
      this.deafCount--;
      this.deafUntil = performance.now() + ECHO_MS;
    }
  }

  // Two quick notes: a clip was kept.
  chime(): Promise<void> {
    const t = this.ctx.currentTime;
    const gain = this.ctx.createGain();
    gain.connect(this.ctx.destination);
    for (const [i, hz] of [880, 1320].entries()) {
      const start = t + i * 0.12;
      const osc = this.ctx.createOscillator();
      osc.frequency.value = hz;
      osc.connect(gain);
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.2, start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.11);
      osc.start(start);
      osc.stop(start + 0.12);
    }
    return this.deafWhile(new Promise((resolve) => setTimeout(resolve, 260)));
  }

  close(): void {
    for (const t of this.stream.getTracks()) t.stop();
    void this.ctx.close();
    void holdWakeMic(false);
  }

  private push(chunk: Float32Array): void {
    if (this.ratio === 1) {
      for (const s of chunk) this.sample(s);
      return;
    }
    // Linear interpolation. `at` is where the next sample falls, counted
    // from this chunk's start; -1 is the last sample of the chunk before.
    while (this.at < chunk.length - 1) {
      const i = Math.floor(this.at);
      const a = i < 0 ? this.prev : chunk[i];
      this.sample(a + (chunk[i + 1] - a) * (this.at - i));
      this.at += this.ratio;
    }
    this.at -= chunk.length;
    this.prev = chunk[chunk.length - 1];
  }

  private sample(s: number): void {
    this.frame[this.filled++] = s;
    if (this.filled < FRAME) return;
    this.filled = 0;
    this.onFrame(this.frame.slice());
  }

  private onFrame(frame: Float32Array): void {
    let sum = 0;
    let peak = 0;
    for (const s of frame) {
      sum += s * s;
      peak = Math.max(peak, Math.abs(s));
    }
    const rms = Math.sqrt(sum / FRAME);
    this.events.level(rms, this.floor);
    if (this.deafCount > 0 || performance.now() < this.deafUntil) return;

    if (this.calibrated < CALIBRATE_FRAMES) {
      this.floor += (rms - this.floor) / ++this.calibrated;
      return;
    }
    if (this.talk) {
      this.talk.push(frame);
      this.talkLength += FRAME;
      if (this.talkLength >= TALK_PIECE) {
        this.events.piece(join(this.talk));
        this.talk = [];
        this.talkLength = 0;
      }
      return;
    }

    if (!this.speech) {
      this.recent.push(frame);
      if (this.recent.length > PAD_FRAMES) this.recent.shift();
      if (rms > Math.max(this.floor * this.sensitivity.over, this.sensitivity.start)) {
        this.loudRun++;
      } else {
        this.loudRun = 0;
        this.floor = Math.max(this.floor * 0.97 + rms * 0.03, 1e-4);
      }
      if (this.loudRun < START_FRAMES) return;
      this.speech = [...this.recent];
      this.quietRun = 0;
      this.maxRms = rms;
      this.peak = peak;
      return;
    }

    this.speech.push(frame);
    this.maxRms = Math.max(this.maxRms, rms);
    this.peak = Math.max(this.peak, peak);
    this.quietRun = rms < Math.max(this.floor * Math.min(2, this.sensitivity.over * 0.7), this.sensitivity.end) ? this.quietRun + 1 : 0;
    if (this.quietRun < END_FRAMES && this.speech.length < MAX_FRAMES + PAD_FRAMES) return;

    const frames = this.speech.slice(0, this.speech.length - Math.max(0, this.quietRun - PAD_FRAMES));
    const speechMs = (this.speech.length - PAD_FRAMES - this.quietRun) * 20;
    const u = { samples: join(frames), speechMs, peak: this.peak, snr: this.maxRms / this.floor };
    this.speech = null;
    this.loudRun = 0;
    this.recent = [];
    if (speechMs >= MIN_SPEECH_MS) this.events.utterance(u);
  }
}

function join(frames: Float32Array[]): Float32Array {
  const out = new Float32Array(frames.reduce((n, f) => n + f.length, 0));
  let at = 0;
  for (const f of frames) {
    out.set(f, at);
    at += f.length;
  }
  return out;
}

// 16-bit PCM WAV, mono, at RATE.
export function wav(samples: Float32Array): Blob {
  const view = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const text = (at: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(at + i, s.charCodeAt(i));
  };
  text(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, RATE, true);
  view.setUint32(28, RATE * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([view], { type: "audio/wav" });
}
