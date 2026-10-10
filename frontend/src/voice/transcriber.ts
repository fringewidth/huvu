import { call, getJson, HttpError } from "../net/http";
import { acquireMic, micTrouble, type Mic } from "./mic";

// Speech to text for voice mode. When the backend can hear (Groq's
// Whisper behind POST /api/transcribe, see GET /api/speech), the ask is
// recorded here and sent there once it ends. Otherwise it falls back to
// the browser's Web Speech API where it exists (desktop Chrome). Where it
// doesn't (the Android WebView), a dev build has a mock that "hears" one
// of a few fixed phrases, and any other build says it can't hear. While
// the backend doesn't answer, nothing listens: an ask couldn't be sent
// anyway. No words show while you speak: only the final text, once the
// ask is over.
export interface ListenOptions {
  // true: keep listening until stop() (push-to-talk). false: the
  // utterance ends by itself on a pause (hands-free).
  hold: boolean;
  // Fires once, when speech first starts.
  onSpeech: () => void;
  // Fires once per utterance with what was heard, possibly empty.
  onEnd: (text: string) => void;
  // Fires instead of onEnd when the ask can't be heard at all (the mic
  // can't be opened, huvu isn't answering), with why, in words for the
  // user.
  onFail: (reason: string) => void;
}

export interface Transcriber {
  start(opts: ListenOptions): void;
  // Ends the utterance. onEnd fires with what was heard.
  stop(): void;
  // Drops the utterance. onEnd does not fire.
  abort(): void;
}

interface RecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onspeechstart: (() => void) | null;
  onend: (() => void) | null;
  onerror: (() => void) | null;
}

type RecognitionCtor = new () => RecognitionLike;

export function createTranscriber(): Transcriber {
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  const Ctor = w.SpeechRecognition ?? w.webkitSpeechRecognition;
  const fallback = Ctor ? webSpeechTranscriber(Ctor) : import.meta.env.DEV ? mockTranscriber() : null;
  // On the web, the browser listens alongside the recording, and its
  // words stand in if Groq can't hear the clip.
  const backend = backendTranscriber(Ctor ? webSpeechTranscriber(Ctor) : null);
  // Which one the latest start picked, once the backend has said. Until
  // then a stop waits for it.
  let active: Transcriber | null = null;
  let latest = 0;
  let stopEarly = false;
  return {
    start(opts) {
      const n = ++latest;
      active?.abort();
      active = null;
      stopEarly = false;
      void canHear().then((hears) => {
        if (n !== latest) return;
        if (hears === "down") return opts.onFail(DOWN);
        const picked = hears === "yes" ? backend : fallback;
        if (!picked) return opts.onFail("huvu can't hear on this device");
        active = picked;
        active.start(opts);
        if (stopEarly) active.stop();
      });
    },
    stop() {
      if (active) active.stop();
      else stopEarly = true;
    },
    abort() {
      latest++;
      active?.abort();
      active = null;
    },
  };
}

// The hint under the flower while the backend doesn't answer.
const DOWN = "huvu isn't answering";

// Whether the backend can hear: "no" when it answered without a Groq key,
// "down" when it didn't answer. Asked once; a failed ask is asked again
// next time.
let hearing: Promise<"yes" | "no" | "down"> | null = null;
function canHear(): Promise<"yes" | "no" | "down"> {
  hearing ??= getJson<{ listen: boolean }>("/api/speech")
    .then((s) => (s.listen ? ("yes" as const) : ("no" as const)))
    .catch(() => {
      hearing = null;
      return "down" as const;
    });
  return hearing;
}

// Mic loudness that counts as speech, and how long a pause after speech
// ends a hands-free ask.
const SPEECH_RMS = 0.03;
const PAUSE_MS = 3000;
// Hands-free, the clip goes to be transcribed once the speaker has been
// quiet this long, not when the pause ends the ask. A phone's round trip
// through the tunnel and Groq is 1 to 2.5s, so the text is usually back
// before PAUSE_MS is up and shows the moment the mic stops. Speech after
// it was sent makes that transcript stale, and the clip is sent again.
const EARLY_MS = 600;
// Opus at this rate is plenty for Whisper to hear speech, and keeps an ask
// to about 20 KB: one round trip through the tunnel to upload, not three.
const BITRATE = 24_000;
// A hands-free listen that hears nothing gives up after this long.
const NOTHING_MS = 6000;

function recordingType(): string {
  for (const type of ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"]) {
    if (MediaRecorder.isTypeSupported(type)) return type;
  }
  return "";
}

// Records an ask from the shared mic, ends it on a pause (hands-free) or
// on stop() (push-to-talk), and sends it to the backend, where Whisper
// takes the whole clip. With a `shadow` (the browser's own recognition),
// its text is used when the backend can't hear the clip (Groq's rate
// limit, a network error). Its words never show while you speak: Chrome
// often gets them wrong, and Whisper's fix of them distracted. The phone has
// no shadow, so there a failed clip ends the listen, and the hint under
// the flower says why.
function backendTranscriber(shadow: Transcriber | null): Transcriber {
  interface Session {
    dropped: boolean;
    stopped: boolean;
    heard: boolean;
    recorder: MediaRecorder | null;
    raf: number;
    shadowHeard?: Promise<string>;
    // Why the backend couldn't hear the clip.
    trouble?: string;
  }
  let session: Session | null = null;

  function start({ hold, onSpeech, onEnd, onFail }: ListenOptions): void {
    abort();
    const own: Session = { dropped: false, stopped: false, heard: false, recorder: null, raf: 0 };
    session = own;
    // What the browser heard, once it has stopped. It ends only when told,
    // since the recording decides when the ask is over.
    let shadowText: (text: string) => void = () => {};
    own.shadowHeard = new Promise((resolve) => (shadowText = resolve));
    shadow?.start({ hold: true, onSpeech: () => {}, onEnd: shadowText, onFail: () => shadowText("") });
    if (!shadow) shadowText("");
    acquireMic()
      .then((mic) => record(own, mic, hold, onSpeech, onEnd, onFail))
      .catch((err) => {
        console.error("transcriber: no mic", err);
        if (!own.dropped) onFail(micTrouble(err));
      });
  }

  function record(own: Session, mic: Mic, hold: boolean, onSpeech: () => void, onEnd: (text: string) => void, onFail: (reason: string) => void): void {
    if (own.dropped) return mic.release();
    const type = recordingType();
    const recorder = new MediaRecorder(mic.stream, { ...(type ? { mimeType: type } : {}), audioBitsPerSecond: BITRATE });
    // The transcript sent during the pause, while nothing has been said
    // since.
    let early: Promise<string | null> | null = null;
    const chunks: Blob[] = [];
    own.recorder = recorder;
    recorder.ondataavailable = (e) => {
      if (e.data.size) chunks.push(e.data);
    };
    recorder.onstop = () => {
      cancelAnimationFrame(own.raf);
      mic.release();
      if (own.dropped) return;
      if (session === own) session = null;
      // Nothing above the noise: Whisper would make up words for silence.
      if (!own.heard) {
        shadow?.abort();
        return onEnd("");
      }
      shadow?.stop();
      const clip = new Blob(chunks, { type: recorder.mimeType });
      const heardAll = early ? early.then((heard) => heard ?? send(own, clip)) : send(own, clip);
      void heardAll.then(async (heard) => {
        if (own.dropped) return;
        const text = heard ?? (await own.shadowHeard) ?? "";
        if (own.dropped) return;
        // Neither Whisper nor the browser heard it. That isn't silence,
        // so hands-free shouldn't listen again as if it were.
        if (heard === null && !text) return onFail(own.trouble ?? DOWN);
        onEnd(text);
      });
    };
    recorder.start(250);
    if (own.stopped) return recorder.stop();

    const began = performance.now();
    let quietSince = 0;
    const watch = () => {
      if (recorder.state !== "recording") return;
      const now = performance.now();
      if (mic.rms() > SPEECH_RMS) {
        if (!own.heard) onSpeech();
        own.heard = true;
        quietSince = 0;
        early = null;
      } else if (own.heard && !hold) {
        quietSince ||= now;
        // The recorder hands over a chunk every 250ms, so the clip so far
        // holds all the speech and a little of the quiet after it.
        if (!early && now - quietSince > EARLY_MS) early = send(own, new Blob(chunks, { type: recorder.mimeType }));
        if (now - quietSince > PAUSE_MS) return recorder.stop();
      }
      if (!own.heard && !hold && now - began > NOTHING_MS) return recorder.stop();
      own.raf = requestAnimationFrame(watch);
    };
    watch();
  }

  // Whisper's text, or null when the backend couldn't hear the clip; then
  // the session keeps why.
  async function send(own: Session, clip: Blob): Promise<string | null> {
    try {
      const res = await call("/api/transcribe", { method: "POST", body: clip, headers: { "Content-Type": clip.type || "audio/webm" } });
      return ((await res.json()) as { text: string }).text.trim();
    } catch (err) {
      console.warn("transcriber: the backend couldn't hear it", err);
      own.trouble = err instanceof HttpError ? "huvu couldn't hear that" : DOWN;
      return null;
    }
  }

  function stop(): void {
    if (!session) return;
    session.stopped = true;
    if (session.recorder?.state === "recording") session.recorder.stop();
  }

  function abort(): void {
    if (!session) return;
    session.dropped = true;
    shadow?.abort();
    if (session.recorder?.state === "recording") session.recorder.stop();
    session = null;
  }

  return { start, stop, abort };
}

function webSpeechTranscriber(Ctor: RecognitionCtor): Transcriber {
  let session: { rec: RecognitionLike; dropped: boolean } | null = null;

  function start({ hold, onSpeech, onEnd }: ListenOptions): void {
    abort();
    const rec = new Ctor();
    rec.continuous = hold;
    rec.interimResults = false;
    rec.lang = navigator.language || "en-IN";
    const own = { rec, dropped: false };
    let text = "";
    rec.onspeechstart = () => own.dropped || onSpeech();
    rec.onresult = (e) => {
      text = Array.from(e.results, (r) => r[0].transcript).join("").trim();
    };
    // Every error is followed by onend, so errors need no handling of
    // their own: an utterance that failed simply ends with what it had.
    rec.onerror = () => {};
    rec.onend = () => {
      if (session === own) session = null;
      if (!own.dropped) onEnd(text);
    };
    session = own;
    rec.start();
  }

  function stop(): void {
    session?.rec.stop();
  }

  function abort(): void {
    if (!session) return;
    session.dropped = true;
    session.rec.abort();
    session = null;
  }

  return { start, stop, abort };
}

const MOCK_PHRASES = [
  "hey huvu, what's blooming today?",
  "remind me to call amma at seven",
  "will it rain this evening?",
];
const MOCK_WORD_MS = 260;
// Silent ticks after the last word before a hands-free utterance ends.
const MOCK_PAUSE_TICKS = 3;

function mockTranscriber(): Transcriber {
  let session: { timer: number; words: string[]; onEnd: (text: string) => void } | null = null;

  function finish(): void {
    if (!session) return;
    const { words, onEnd } = session;
    window.clearInterval(session.timer);
    session = null;
    // The mock always "heard" the whole phrase, however short the hold.
    onEnd(words.join(" "));
  }

  function start({ hold, onSpeech, onEnd }: ListenOptions): void {
    abort();
    const words = MOCK_PHRASES[Math.floor(Math.random() * MOCK_PHRASES.length)].split(" ");
    let ticks = 0;
    const timer = window.setInterval(() => {
      ticks++;
      if (ticks === 1) onSpeech();
      if (!hold && ticks >= words.length + MOCK_PAUSE_TICKS) finish();
    }, MOCK_WORD_MS);
    session = { timer, words, onEnd };
  }

  function abort(): void {
    if (!session) return;
    window.clearInterval(session.timer);
    session = null;
  }

  return { start, stop: finish, abort };
}
