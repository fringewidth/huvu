// One AudioContext for the whole app: huvu's voice and the mic share it.
// Each new context spins up its own audio thread, which costs tens of
// milliseconds on Android, so it is made once and kept.
let ctx: AudioContext | null = null;

export function audioContext(): AudioContext {
  ctx ??= new AudioContext();
  return ctx;
}

// Call from a user gesture (the flower, a push-to-talk key). A context
// resumed inside a gesture is never held back by the autoplay policy.
export function resumeAudio(): void {
  void audioContext().resume();
}

export function rms(analyser: AnalyserNode, buf: Uint8Array<ArrayBuffer>): number {
  analyser.getByteTimeDomainData(buf);
  let sum = 0;
  for (let i = 0; i < buf.length; i++) {
    const v = (buf[i] - 128) / 128;
    sum += v * v;
  }
  return Math.sqrt(sum / buf.length);
}
