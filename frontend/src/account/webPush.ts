import { useSyncExternalStore } from "react";
import { Capacitor } from "@capacitor/core";
import { getJson, postJson } from "../net/http";
import { opened } from "./opens";

// Web Push for huvu added to a phone's home screen, which logs in as a
// phone (HOME_SCREEN in config/interaction.ts) and has no FCM: what
// nativePush.ts does for the app. The service worker (public/sw.js) shows
// each push, and the backend sends the same events it sends to FCM
// (backend/src/huvu/web_push.py).
//
// iOS asks for permission only from a tap, so the menu has a button for it
// (MenuPanel.tsx). Once allowed, each start hands the backend the current
// subscription again, since a push service can change it.

// "unsupported" in a browser tab, on the TV and in the native app.
export type WebPushState = "unsupported" | "off" | "on" | "blocked";

let state: WebPushState = "unsupported";
let started = false;
const listeners = new Set<() => void>();

function set(next: WebPushState): void {
  state = next;
  for (const listener of listeners) listener();
}

function supported(): boolean {
  return !Capacitor.isNativePlatform() && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

function fromPermission(): WebPushState {
  return Notification.permission === "granted" ? "on" : Notification.permission === "denied" ? "blocked" : "off";
}

export async function startWebPush(): Promise<void> {
  if (!supported() || started) return;
  started = true;
  try {
    await navigator.serviceWorker.register(`${import.meta.env.BASE_URL}sw.js`);
    navigator.serviceWorker.addEventListener("message", onMessage);
    document.addEventListener("visibilitychange", clearTray);
    void clearTray();
    set(fromPermission());
    if (state === "on") await subscribe();
  } catch (err) {
    console.error("web push: could not start", err);
  }
}

// The device logged out, and the backend dropped its subscription with it.
// The browser keeps it, and the next login hands it over again.
export function stopWebPush(): void {
  if (!started) return;
  started = false;
  navigator.serviceWorker.removeEventListener("message", onMessage);
  document.removeEventListener("visibilitychange", clearTray);
}

// Called from a tap, and asks before anything else is awaited, since iOS
// takes the request only inside the tap.
export async function turnOnWebPush(): Promise<void> {
  const permission = await Notification.requestPermission();
  set(fromPermission());
  if (permission === "granted") await subscribe();
}

export function useWebPush(): WebPushState {
  return useSyncExternalStore(subscribeState, () => state);
}

function subscribeState(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

async function subscribe(): Promise<void> {
  const registration = await navigator.serviceWorker.ready;
  const { key } = await getJson<{ key: string }>("/api/push/web");
  const serverKey = decode(key);
  let subscription = await registration.pushManager.getSubscription();
  // A subscription made with another key (the host's data was reset) can't
  // be used.
  if (subscription && !sameKey(subscription.options.applicationServerKey, serverKey)) {
    await subscription.unsubscribe();
    subscription = null;
  }
  subscription ??= await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: serverKey });
  await postJson("/api/push/web", subscription.toJSON());
}

// A tap on a notification while huvu was already open.
function onMessage(event: MessageEvent): void {
  const data = event.data as { type?: string } | undefined;
  if (data?.type === "opened") opened(data);
}

// Once huvu is in front it shows everything itself, so the tray is cleared.
async function clearTray(): Promise<void> {
  if (document.visibilityState !== "visible") return;
  const registration = await navigator.serviceWorker.getRegistration();
  for (const notification of (await registration?.getNotifications()) ?? []) notification.close();
}

function decode(key: string): Uint8Array<ArrayBuffer> {
  const raw = atob(key.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

// A browser that doesn't say which key a subscription has is trusted.
function sameKey(current: ArrayBuffer | null | undefined, wanted: Uint8Array): boolean {
  if (current == null) return true;
  const bytes = new Uint8Array(current);
  return bytes.length === wanted.length && bytes.every((b, i) => b === wanted[i]);
}
