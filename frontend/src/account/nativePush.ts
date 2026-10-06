import { Capacitor, type PluginListenerHandle } from "@capacitor/core";
import { postJson } from "../net/http";

// Native push on a phone, through FCM (APNs behind it on iOS). The push
// stream reaches a phone only while the app holds it open, so the backend
// also sends what needs a person to this phone's FCM token: notifications,
// approvals and "is it you?" questions. It sends them even while the app
// is open, and the OS shows nothing to the app in front (Android by
// default, iOS through `presentationOptions` in capacitor.config.ts).
//
// Once the app is in front it shows all of it itself, so the tray is
// cleared. A tap on a notification only opens the app: approvals and
// questions wait in their card, and an unseen notice takes the stage. A
// notice that names a screen (`opens`) opens it.
//
// The plugin loads only on a native phone, so web and the TV never fetch
// it.

type Messaging = typeof import("@capacitor-firebase/messaging");

// The screens a notice can open. The backend names them in `opens`.
export type Opens = "triggers";

let handles: PluginListenerHandle[] = [];
let opener: ((opens: Opens) => void) | null = null;
// A tap that came before anyone listened: a cold start from the tray.
let pending: Opens | null = null;

export async function startNativePush(): Promise<void> {
  if (!Capacitor.isNativePlatform() || handles.length > 0) return;
  try {
    const { FirebaseMessaging, Importance } = (await import("@capacitor-firebase/messaging")) as Messaging;
    const { receive } = await FirebaseMessaging.requestPermissions();
    if (receive !== "granted") return;
    if (Capacitor.getPlatform() === "android") {
      // The channel ids are the ones the backend names (native_push.py).
      await FirebaseMessaging.createChannel({ id: "huvu", name: "From huvu", importance: Importance.Default });
      await FirebaseMessaging.createChannel({ id: "huvu.urgent", name: "Needs you", importance: Importance.High });
    }
    handles = await Promise.all([
      FirebaseMessaging.addListener("tokenReceived", ({ token }) => void register(token)),
      FirebaseMessaging.addListener("notificationActionPerformed", ({ notification }) => opened(notification.data)),
    ]);
    document.addEventListener("visibilitychange", clearTray);
    void clearTray();
    await register((await FirebaseMessaging.getToken()).token);
  } catch (err) {
    console.error("native push: could not start", err);
  }
}

// The device logged out, and the backend dropped its token with it.
export async function stopNativePush(): Promise<void> {
  document.removeEventListener("visibilitychange", clearTray);
  const stopping = handles;
  handles = [];
  pending = null;
  await Promise.all(stopping.map((h) => h.remove()));
}

// The stage opens what a tapped notice names. Returns the unsubscribe.
export function onOpens(handler: (opens: Opens) => void): () => void {
  opener = handler;
  if (pending) handler(pending);
  pending = null;
  return () => {
    if (opener === handler) opener = null;
  };
}

async function register(token: string): Promise<void> {
  try {
    await postJson("/api/push/token", { token, platform: Capacitor.getPlatform() });
  } catch (err) {
    console.error("native push: could not register the token", err);
  }
}

function opened(data: unknown): void {
  const opens = (data as { opens?: unknown } | undefined)?.opens;
  if (opens !== "triggers") return;
  if (opener) opener(opens);
  else pending = opens;
}

async function clearTray(): Promise<void> {
  if (document.visibilityState !== "visible") return;
  const { FirebaseMessaging } = (await import("@capacitor-firebase/messaging")) as Messaging;
  await FirebaseMessaging.removeAllDeliveredNotifications().catch(() => {});
}
