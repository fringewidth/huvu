import { Capacitor, type PluginListenerHandle } from "@capacitor/core";
import { postJson } from "../net/http";
import { opened } from "./opens";

// Native push on a phone, through FCM (APNs behind it on iOS). The push
// stream reaches a phone only while the app holds it open, so the backend
// also sends what needs a person to this phone's FCM token: notifications,
// approvals and "is it you?" questions. It sends them even while the app
// is open, and the OS shows nothing to the app in front (Android by
// default, iOS through `presentationOptions` in capacitor.config.ts).
//
// Once the app is in front it shows all of it itself, so the tray is
// cleared. What a tap opens is in opens.ts.
//
// The plugin loads only on a native phone, so web and the TV never fetch
// it. A home screen web app gets the same through webPush.ts.

type Messaging = typeof import("@capacitor-firebase/messaging");

let handles: PluginListenerHandle[] = [];

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
  await Promise.all(stopping.map((h) => h.remove()));
}

async function register(token: string): Promise<void> {
  try {
    await postJson("/api/push/token", { token, platform: Capacitor.getPlatform() });
  } catch (err) {
    console.error("native push: could not register the token", err);
  }
}

async function clearTray(): Promise<void> {
  if (document.visibilityState !== "visible") return;
  const { FirebaseMessaging } = (await import("@capacitor-firebase/messaging")) as Messaging;
  await FirebaseMessaging.removeAllDeliveredNotifications().catch(() => {});
}
