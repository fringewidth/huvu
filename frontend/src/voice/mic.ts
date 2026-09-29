import { audioContext, rms } from "./audio";

// The mic, shared by the level meter (while you talk) and barge-in (while
// huvu talks). getUserMedia costs 100ms or more per call, so the stream
// stays open for a while after its last user lets go. The next listen or
// the next reply picks it up at once.
export interface Mic {
  rms(): number;
  release(): void;
}

const LINGER_MS = 8000;

interface Shared {
  stream: MediaStream;
  source: MediaStreamAudioSourceNode;
  analyser: AnalyserNode;
  buf: Uint8Array<ArrayBuffer>;
}

let open: Promise<Shared> | null = null;
let users = 0;
let closeTimer = 0;
let closeSoon = false;

function close(): void {
  const closing = open;
  open = null;
  closing
    ?.then(({ stream, source }) => {
      source.disconnect();
      for (const t of stream.getTracks()) t.stop();
    })
    .catch(() => {});
}

export async function acquireMic(): Promise<Mic> {
  window.clearTimeout(closeTimer);
  closeSoon = false;
  users++;
  open ??= navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => {
    const ctx = audioContext();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    const source = ctx.createMediaStreamSource(stream);
    source.connect(analyser);
    return { stream, source, analyser, buf: new Uint8Array(analyser.fftSize) };
  });
  let shared;
  try {
    shared = await open;
  } catch (err) {
    users--;
    open = null;
    throw err;
  }
  let released = false;
  return {
    rms: () => rms(shared.analyser, shared.buf),
    release: () => {
      if (released) return;
      released = true;
      if (--users > 0) return;
      if (closeSoon) close();
      else closeTimer = window.setTimeout(close, LINGER_MS);
    },
  };
}

// Leaving voice mode closes the mic as soon as nothing uses it, with no
// linger, so the recording light goes off.
export function closeMic(): void {
  window.clearTimeout(closeTimer);
  if (users > 0) closeSoon = true;
  else close();
}
