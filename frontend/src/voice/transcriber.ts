// Speech to text for voice mode. Uses the browser's Web Speech API where
// it exists (desktop Chrome). The Android WebView and Fire TV have no
// such API, so they get a mock that "hears" one of a few fixed phrases,
// until a real STT service sits behind the gateway.
export interface ListenOptions {
  // true: keep listening until stop() (push-to-talk). false: the
  // utterance ends by itself on a pause (hands-free).
  hold: boolean;
  onText: (text: string) => void;
  // Fires once per utterance with what was heard, possibly empty.
  onEnd: (text: string) => void;
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
  onend: (() => void) | null;
  onerror: (() => void) | null;
}

type RecognitionCtor = new () => RecognitionLike;

export function createTranscriber(): Transcriber {
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  const Ctor = w.SpeechRecognition ?? w.webkitSpeechRecognition;
  return Ctor ? webSpeechTranscriber(Ctor) : mockTranscriber();
}

function webSpeechTranscriber(Ctor: RecognitionCtor): Transcriber {
  let session: { rec: RecognitionLike; dropped: boolean } | null = null;

  function start({ hold, onText, onEnd }: ListenOptions): void {
    abort();
    const rec = new Ctor();
    rec.continuous = hold;
    rec.interimResults = true;
    rec.lang = navigator.language || "en-IN";
    const own = { rec, dropped: false };
    let text = "";
    rec.onresult = (e) => {
      text = Array.from(e.results, (r) => r[0].transcript).join("").trim();
      onText(text);
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

  function start({ hold, onText, onEnd }: ListenOptions): void {
    abort();
    const words = MOCK_PHRASES[Math.floor(Math.random() * MOCK_PHRASES.length)].split(" ");
    let ticks = 0;
    const timer = window.setInterval(() => {
      ticks++;
      if (ticks <= words.length) onText(words.slice(0, ticks).join(" "));
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
