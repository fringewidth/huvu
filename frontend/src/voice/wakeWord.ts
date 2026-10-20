import { useSyncExternalStore } from "react";
import { Capacitor, registerPlugin, type PluginListenerHandle } from "@capacitor/core";

// "hey huvu", on the Android app only: a platform check, not a target one,
// since iOS lets no app keep the mic in the background. The app's own
// plugin (android/app/src/main/java/com/huvu/app/wake/) listens with
// openWakeWord in a foreground service while the screen is on and
// unlocked, and brings huvu to the front when it hears the phrase. Hearing
// it does what a tap on the flower does (Stage.tsx).
//
// The plugin keeps whether it is on, so it stays on across restarts. The
// menu turns it on and off (MenuPanel.tsx). The service lets go of the mic
// while the page records (holdWakeMic), since two recordings in one app
// can leave the WebView's with silence.

interface NativeState {
  on: boolean;
  // Whether huvu may show over other apps, which Android asks of any app
  // that brings itself to the front from behind another one.
  overlay: boolean;
}

interface WakeWordPlugin {
  state(): Promise<NativeState>;
  turnOn(): Promise<NativeState>;
  turnOff(): Promise<NativeState>;
  allowOverlay(): Promise<void>;
  hold(options: { held: boolean }): Promise<void>;
  addListener(event: "heard", listener: () => void): Promise<PluginListenerHandle>;
}

export const WAKE_WORD = Capacitor.getPlatform() === "android";

const plugin = registerPlugin<WakeWordPlugin>("WakeWord");

// null until the plugin first answers.
export type WakeWordState = NativeState | null;

let state: WakeWordState = null;
const listeners = new Set<() => void>();
const heardListeners = new Set<() => void>();

function set(next: WakeWordState): void {
  state = next;
  for (const listener of listeners) listener();
}

async function refresh(): Promise<void> {
  try {
    set(await plugin.state());
  } catch (err) {
    console.error("wake word: couldn't read its state", err);
  }
}

if (WAKE_WORD) {
  void plugin.addListener("heard", () => {
    for (const listener of heardListeners) listener();
  });
  void refresh();
  // Coming back from Android's settings, where the mic or showing over
  // other apps may have changed.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") void refresh();
  });
}

export function useWakeWord(): WakeWordState {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => state,
  );
}

export function onWakeWord(listener: () => void): () => void {
  heardListeners.add(listener);
  return () => heardListeners.delete(listener);
}

// Rejects with why it can't, in the words the menu shows.
export async function turnOnWakeWord(): Promise<void> {
  set(await plugin.turnOn());
}

export async function turnOffWakeWord(): Promise<void> {
  set(await plugin.turnOff());
}

export async function allowOverApps(): Promise<void> {
  await plugin.allowOverlay();
}

// What in this page holds the mic: voice mode's shared mic (mic.ts) and
// the recording screen (clipRecorder.ts). Each hold is let go once. The
// listener gets the mic back when the last one lets go, and the hand-overs
// reach the plugin in order.
let holders = 0;
// Nothing holds the mic yet. The page before a reload (a live update) may
// have left it held.
let handing = WAKE_WORD ? plugin.hold({ held: false }).catch(() => {}) : Promise.resolve();

// Taking the mic resolves once the listener has stopped recording.
export function holdWakeMic(held: boolean): Promise<void> {
  if (!WAKE_WORD) return Promise.resolve();
  holders = Math.max(0, holders + (held ? 1 : -1));
  if (holders === (held ? 1 : 0)) {
    handing = handing.then(() => plugin.hold({ held }).catch((err: unknown) => console.error("wake word: couldn't hand over the mic", err)));
  }
  return handing;
}
