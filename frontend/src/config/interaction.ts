export type Target = "web" | "phone" | "firetv";
export type Mode = "text" | "voice";

// Everything about how the UI behaves on one build target. This answers
// "what does this screen look like". What the device can do (wake word is
// Android only) is a platform check, kept apart from this on purpose.
interface Profile {
  defaultMode: Mode;
  // Fire TV is voice only, with no toggle rendered at all.
  modeSwitchable: boolean;
  // Enter sends on web. Phone keyboards put Enter where a new line
  // belongs, so phone sends only with its send button.
  enterSends: boolean;
  sendButton: boolean;
  // The key held down to talk. Release sends. Phone has no hardware key
  // for this, so the flower is the only way into voice there. Fire TV's
  // select button arrives in the WebView as Enter.
  pushToTalkKey: string | null;
  placeholder: string;
  idleHint: string;
  // Fire TV has no text, so editing a turn there means asking it again.
  editLabel: string;
  // The voice flower's full size, in px.
  flowerSize: number;
  // Whether huvu's answer streams in as text above the flower in voice
  // mode. A phone screen has no room for it next to the flower and the
  // transcript, and huvu says the answer anyway.
  voiceAnswerText: boolean;
  // Phone screens are dense enough that 1.5x looks like 2x, at almost half
  // the pixels for the garden to wash every frame.
  maxPixelDensity: number;
}

const PROFILES: Record<Target, Profile> = {
  web: {
    defaultMode: "text",
    modeSwitchable: true,
    enterSends: true,
    sendButton: false,
    pushToTalkKey: " ",
    placeholder: "write, or hold space to speak",
    idleHint: "hold space to speak, or click the flower",
    editLabel: "edit",
    flowerSize: 168,
    voiceAnswerText: true,
    maxPixelDensity: 2,
  },
  phone: {
    defaultMode: "text",
    modeSwitchable: true,
    enterSends: false,
    sendButton: true,
    pushToTalkKey: null,
    placeholder: "write, or tap the flower",
    idleHint: "tap the flower to listen",
    editLabel: "edit",
    flowerSize: 168,
    voiceAnswerText: false,
    maxPixelDensity: 1.5,
  },
  firetv: {
    defaultMode: "voice",
    modeSwitchable: false,
    enterSends: false,
    sendButton: false,
    pushToTalkKey: "Enter",
    placeholder: "",
    idleHint: "hold select to speak",
    editLabel: "rewind",
    flowerSize: 200,
    voiceAnswerText: true,
    maxPixelDensity: 2,
  },
};

// No default and no fallback: every script names its target.
function resolveTarget(): Target {
  const raw = import.meta.env.VITE_TARGET;
  if (!Object.hasOwn(PROFILES, raw)) {
    throw new Error(`VITE_TARGET must be one of ${Object.keys(PROFILES).join(", ")}, got ${JSON.stringify(raw)}`);
  }
  return raw;
}

export const TARGET: Target = resolveTarget();
export const ui: Profile = PROFILES[TARGET];
export const TV = TARGET === "firetv";

// The phone widget's quick-voice button deep-links with ?mode=voice.
export function initialMode(): Mode {
  const requested = new URLSearchParams(window.location.search).get("mode");
  return ui.modeSwitchable && (requested === "text" || requested === "voice") ? requested : ui.defaultMode;
}
