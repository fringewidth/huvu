export type Target = "web" | "phone" | "firetv";
export type Mode = "text" | "voice" | "none";

// What the stage says before there is any turn. `example` is an ask to
// try, in your words.
export interface Greeting {
  line: string;
  example?: string;
}

// Everything about how the UI behaves on one build target. This answers
// "what does this screen look like". What the device can do (wake word is
// Android only) is a platform check, kept apart from this on purpose.
// What kind of device the backend is told this is. A personal device
// (phone, web) logs in with its user's password; a shared one (tv) pairs
// with a code a member confirms on their phone.
export type DeviceKind = "phone" | "web" | "tv";

interface Profile {
  deviceKind: DeviceKind;
  // Whether this screen has input of its own: a composer, the flower, edit
  // and retry. Fire TV has none. It shows the turns of its own
  // conversation (asks reach it through Alexa) and the live feeds picked
  // on it, and still sends a choice picked with the remote.
  hasInput: boolean;
  // Says every answer aloud, in any mode, and sends every ask as spoken,
  // so the answer comes without markdown. Fire TV's asks are spoken to
  // Alexa, and the person is across the room.
  speaksAnswers: boolean;
  // "none" for a screen that asks nothing. initialMode() shows it as text.
  defaultMode: Mode;
  modeSwitchable: boolean;
  // Enter sends on web. Phone keyboards put Enter where a new line
  // belongs, so phone sends only with its send button.
  enterSends: boolean;
  sendButton: boolean;
  // Whether the screen comes with a keyboard of its own: the text field
  // keeps focus in text mode, and typing anywhere lands in it. A touch
  // screen would pop its keyboard up each time.
  keyboard: boolean;
  // The key held down to talk. Release sends. Phone has no hardware key
  // for this, so the flower is the only way into voice there. Left out
  // where there is no such key.
  pushToTalkKey?: string;
  // The key that opens the list of live feeds. Fire TV has no pointer
  // for the LIVE control, so its Menu button opens the list. Chromium
  // reports that button as ContextMenu, unverified on a real Fire TV.
  feedsKey?: string;
  // The voice and text fields below are left out where the screen asks
  // nothing.
  placeholder?: string;
  idleHint?: string;
  // One is picked at random at launch.
  greetings: Greeting[];
  // The voice flower's full size, in px.
  flowerSize?: number;
  // Whether huvu's answer streams in as text above the flower in voice
  // mode. A phone screen has no room for it next to the flower and the
  // transcript, and huvu says the answer anyway.
  voiceAnswerText?: boolean;
  // Phone screens are dense enough that 1.5x looks like 2x, at almost half
  // the pixels for the garden to paint.
  maxPixelDensity: number;
}

const PROFILES: Record<Target, Profile> = {
  web: {
    deviceKind: "web",
    hasInput: true,
    speaksAnswers: false,
    defaultMode: "text",
    modeSwitchable: true,
    enterSends: true,
    sendButton: false,
    keyboard: true,
    pushToTalkKey: " ",
    placeholder: "write, or hold space to speak",
    idleHint: "hold space to speak, or click the flower",
    greetings: [
      { line: "Back at it." },
      { line: "Look who's back." },
      { line: "Let's get going." },
      { line: "Ready when you are." },
      { line: "Ready?" },
      { line: "What's on your mind?" },
    ],
    flowerSize: 168,
    voiceAnswerText: true,
    maxPixelDensity: 2,
  },
  phone: {
    deviceKind: "phone",
    hasInput: true,
    speaksAnswers: false,
    defaultMode: "text",
    modeSwitchable: true,
    enterSends: false,
    sendButton: true,
    keyboard: false,
    placeholder: "write, or tap the flower",
    idleHint: "tap the flower to listen",
    greetings: [
      { line: "Back at it." },
      { line: "Look who's back." },
      { line: "Let's get going." },
      { line: "Ready when you are." },
      { line: "Ready?" },
      { line: "What's on your mind?" },
    ],
    flowerSize: 168,
    voiceAnswerText: false,
    maxPixelDensity: 1.5,
  },
  // Display only. It asks nothing, so the voice and text fields are left
  // out. Asks come through Alexa, and huvu says every answer.
  firetv: {
    deviceKind: "tv",
    hasInput: false,
    speaksAnswers: true,
    defaultMode: "none",
    modeSwitchable: false,
    enterSends: false,
    sendButton: false,
    keyboard: false,
    feedsKey: "ContextMenu",
    // Asks reach the TV through Alexa, so each greeting shows one.
    greetings: [
      { line: "Feeling a classic, or something new?", example: "alexa, ask my huvu for a film like blade runner" },
      { line: "One more episode?", example: "alexa, ask my huvu how long is the first season of Invincible" },
      {
        line: "Sit back and relax.",
        example: (() => {
          const languages = ["Telugu", "Kannada", "Hindi", "Tamil", "Malayalam"];
          return `alexa, ask my huvu what's some cool ${languages[Math.floor(Math.random() * languages.length)]} movies`;
        })(),
      },
      { line: "Got your popcorn?", example: "alexa, ask my huvu what's a good movie under two hours" },
      // Two more for the show the clock is in, read once at launch in the
      // device's local time, the way a cinema runs its day.
      ...(() => {
        const hour = new Date().getHours();
        return hour >= 21 || hour < 5
          ? [
              { line: "The late-night screening is on.", example: "alexa, ask my huvu for something to watch before bed" },
              { line: "Something short tonight?", example: "alexa, ask my huvu for a series with episodes under 30 minutes" },
            ]
          : hour < 12
            ? [
                { line: "The morning show is open.", example: "alexa, ask my huvu for something light to start the day" },
                { line: "Slow start? Pick something easy.", example: "alexa, ask my huvu for a feel-good comedy" },
              ]
            : hour < 17
              ? [
                  { line: "Matinee time.", example: "alexa, ask my huvu what to watch this afternoon" },
                  { line: "An afternoon off deserves a good film.", example: "alexa, ask my huvu for a movie under two hours" },
                ]
              : [
                  { line: "The evening show is about to begin.", example: "alexa, ask my huvu what's worth watching tonight" },
                  { line: "What are we watching tonight?", example: "alexa, ask my huvu what's good on netflix" },
                ];
      })(),
    ],
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
// The web build in a phone's browser: a screen whose main pointer is touch.
// A phone with a pen (a Galaxy Ultra) has a fine pointer too, so only the
// main one counts; a touch laptop's main pointer is its trackpad. It looks
// like a phone, so it takes the phone's screen rules (no space to talk, a
// send button, the phone's hints and garden density). In a browser tab it
// stays a browser to the backend. Added to the home screen (an iPhone, which
// has no huvu app, or an Android phone whose owner can't install one), it
// opens in its own window and logs in as a phone: it gets notifications
// through Web Push (account/webPush.ts), answers approvals and "is it you?",
// and pairs.
const TOUCH_WEB = TARGET === "web" && window.matchMedia("(pointer: coarse)").matches;
const HOME_SCREEN =
  TOUCH_WEB && (window.matchMedia("(display-mode: standalone)").matches || (navigator as Navigator & { standalone?: boolean }).standalone === true);
export const ui: Profile = HOME_SCREEN ? PROFILES.phone : TOUCH_WEB ? { ...PROFILES.phone, deviceKind: "web" } : PROFILES[TARGET];
export const TV = TARGET === "firetv";
// A shared device pairs instead of logging in, and can recognize who is at
// it. A phone answers approvals and "is it you?" questions, and gets
// notifications.
export const SHARED = ui.deviceKind === "tv";
export const PHONE = ui.deviceKind === "phone";

// The phone widget's quick-voice button deep-links with ?mode=voice.
export function initialMode(): Mode {
  const requested = new URLSearchParams(window.location.search).get("mode");
  if (ui.modeSwitchable && (requested === "text" || requested === "voice")) return requested;
  // A screen with no mode shows replies the way text mode does.
  return ui.defaultMode === "none" ? "text" : ui.defaultMode;
}
