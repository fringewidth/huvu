// Audio analysis: rms/onset/centroid/bass, ported from the recovered
// prototype. `state` is read by the render loop every frame; the play*/use*
// methods are wired to UI triggers.
export interface AudioState {
  ctx: AudioContext | null;
  analyser: AnalyserNode | null;
  source: AudioNode | null;
  element: HTMLAudioElement | null;
  stream: MediaStream | null;
  td: Uint8Array<ArrayBuffer> | null;
  fd: Uint8Array<ArrayBuffer> | null;
  rms: number;
  fast: number;
  slow: number;
  peak: number;
  loud: number;
  centroid: number;
  bass: number;
  lastOnset: number;
  active: boolean;
}

export interface AudioEngine {
  state: AudioState;
  playFromUrl(url: string): Promise<void>;
  useMic(): Promise<void>;
  useFile(file: File): Promise<void>;
  disconnectSource(): void;
  analyze(dt: number, now: number): boolean;
}

export function createAudioEngine(): AudioEngine {
  const state: AudioState = {
    ctx: null,
    analyser: null,
    source: null,
    element: null,
    stream: null,
    td: null,
    fd: null,
    rms: 0,
    fast: 0,
    slow: 0,
    peak: 0.05,
    loud: 0,
    centroid: 0.5,
    bass: 0.5,
    lastOnset: -1,
    active: false,
  };

  function ensureContext(): AudioContext {
    if (!state.ctx) {
      state.ctx = new AudioContext();
      state.analyser = state.ctx.createAnalyser();
      state.analyser.fftSize = 1024;
      state.analyser.smoothingTimeConstant = 0.3;
      state.td = new Uint8Array(state.analyser.fftSize);
      state.fd = new Uint8Array(state.analyser.frequencyBinCount);
    }
    return state.ctx;
  }

  function disconnectSource(): void {
    if (state.source) {
      try {
        state.source.disconnect();
      } catch {
        // already disconnected
      }
      state.source = null;
    }
    if (state.element) {
      state.element.pause();
      state.element = null;
    }
    if (state.stream) {
      for (const t of state.stream.getTracks()) t.stop();
      state.stream = null;
    }
    state.active = false;
  }

  // Plays a bundled audio clip (e.g. the `say`-generated test voice) and
  // routes it through the analyser, same path a real Hermes/TTS clip would
  // eventually take.
  async function playFromUrl(url: string): Promise<void> {
    const ctx = ensureContext();
    disconnectSource();
    await ctx.resume();
    const res = await fetch(url);
    const buffer = await ctx.decodeAudioData(await res.arrayBuffer());
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.loop = true;
    src.connect(state.analyser!);
    state.analyser!.connect(ctx.destination);
    src.start();
    state.source = src;
    state.active = true;
  }

  async function useMic(): Promise<void> {
    const ctx = ensureContext();
    disconnectSource();
    await ctx.resume();
    state.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const src = ctx.createMediaStreamSource(state.stream);
    src.connect(state.analyser!);
    state.source = src;
    state.active = true;
  }

  async function useFile(file: File): Promise<void> {
    const ctx = ensureContext();
    disconnectSource();
    await ctx.resume();
    const el = new Audio(URL.createObjectURL(file));
    el.loop = true;
    const src = ctx.createMediaElementSource(el);
    src.connect(state.analyser!);
    state.analyser!.connect(ctx.destination);
    await el.play();
    state.element = el;
    state.source = src;
    state.active = true;
  }

  function analyze(dt: number, now: number): boolean {
    if (!state.active || !state.analyser || !state.td || !state.fd || !state.ctx) {
      state.loud *= Math.max(0, 1 - dt * 6);
      return false;
    }
    state.analyser.getByteTimeDomainData(state.td);
    state.analyser.getByteFrequencyData(state.fd);

    let sum = 0;
    for (let i = 0; i < state.td.length; i++) {
      const v = (state.td[i] - 128) / 128;
      sum += v * v;
    }
    state.rms = Math.sqrt(sum / state.td.length);
    state.peak = Math.max(state.peak * (1 - dt * 0.08), state.rms, 0.04);
    state.loud = Math.min(1, state.rms / state.peak);

    const binHz = state.ctx.sampleRate / state.analyser.fftSize;
    let num = 0, den = 0, bassE = 0, totE = 0;
    const maxBin = Math.min(state.fd.length, Math.floor(6000 / binHz));
    for (let i = 1; i < maxBin; i++) {
      const m = state.fd[i] / 255;
      const e = m * m;
      num += i * binHz * e;
      den += e;
      totE += e;
      if (i * binHz < 320) bassE += e;
    }
    if (den > 1e-4) {
      const hz = num / den;
      const c = (Math.log(hz) - Math.log(180)) / (Math.log(3200) - Math.log(180));
      state.centroid += (Math.min(1, Math.max(0, c)) - state.centroid) * Math.min(1, dt * 8);
    }
    if (totE > 1e-4) state.bass += (bassE / totE - state.bass) * Math.min(1, dt * 8);

    state.fast += (state.rms - state.fast) * Math.min(1, dt * 30);
    state.slow += (state.rms - state.slow) * Math.min(1, dt * 4);
    const onset = state.fast > state.slow * 1.35 + 0.012 && state.loud > 0.18 && now - state.lastOnset > 0.13;
    if (onset) state.lastOnset = now;
    return onset;
  }

  return { state, playFromUrl, useMic, useFile, disconnectSource, analyze };
}
