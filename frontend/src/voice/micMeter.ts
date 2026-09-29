// How loud the user is, 0 to 1, for the voice flower: it grows as you
// talk. It opens its own AudioContext and stream for the same reason as
// listenForBargeIn in canvas/audio.ts: the garden's analyser belongs to
// huvu's voice, and the two must not fight over it.
export interface MicMeter {
  level(): number;
  stop(): void;
}

// Below this share of the recent peak counts as room noise, not speech.
const NOISE_FLOOR = 0.12;

export async function startMicMeter(): Promise<MicMeter> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const ctx = new AudioContext();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 1024;
  ctx.createMediaStreamSource(stream).connect(analyser);
  const td = new Uint8Array(analyser.fftSize);
  let peak = 0.05;

  function level(): number {
    analyser.getByteTimeDomainData(td);
    let sum = 0;
    for (let i = 0; i < td.length; i++) {
      const v = (td[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / td.length);
    peak = Math.max(peak * 0.995, rms, 0.04);
    return Math.max(0, Math.min(1, (rms / peak - NOISE_FLOOR) / (1 - NOISE_FLOOR)));
  }

  function stop(): void {
    for (const t of stream.getTracks()) t.stop();
    ctx.close();
  }

  return { level, stop };
}
