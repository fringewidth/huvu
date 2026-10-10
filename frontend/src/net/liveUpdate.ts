import { App } from "@capacitor/app";
import { Capacitor } from "@capacitor/core";
import { TARGET } from "../config/interaction";
import { BACKEND, getJson } from "./http";

// Live updates for the native apps. An APK carries its own copy of the web
// build, so a frontend change used to reach the phone only through a new
// APK. The host serves each native target's latest build
// (backend/src/huvu/bundles.py). The app checks on launch and each time it
// comes back to the front, downloads a newer build, and switches to it the
// next time it goes to the background, so nothing reloads under the user.
//
// Every build carries bundle.json (vite.config.ts): a hash of its files,
// and the native version it was built for (native.json). A build goes
// only to an APK of that native version. A change on the native side (a
// plugin, the manifest, Java) still needs a new APK, with native.json
// bumped.
//
// The plugin loads only in a native app, so the web build never fetches
// it.

type Updater = (typeof import("@capgo/capacitor-updater"))["CapacitorUpdater"];

interface Stamp {
  version: string;
  native: number;
}

let checking = false;

export function startLiveUpdates(): void {
  if (!Capacitor.isNativePlatform()) return;
  void (async () => {
    const { CapacitorUpdater } = await import("@capgo/capacitor-updater");
    // This build started. A downloaded build that doesn't say so within
    // 10s is dropped, and the app goes back to the build before it.
    await CapacitorUpdater.notifyAppReady();
    void check(CapacitorUpdater);
    await App.addListener("resume", () => void check(CapacitorUpdater));
  })().catch((err) => console.error("live update: couldn't start", err));
}

async function check(updater: Updater): Promise<void> {
  if (checking) return;
  checking = true;
  try {
    const [own, latest, { native }] = await Promise.all([
      fetch("/bundle.json").then((res) => res.json() as Promise<Stamp>),
      getJson<Stamp>(`/api/app/${TARGET}`),
      updater.current(),
    ]);
    if (latest.version === own.version) return;
    if (String(latest.native) !== native) {
      console.info(`live update: build ${latest.version} needs native version ${latest.native}, this APK is ${native}`);
      return;
    }
    const { bundles } = await updater.list();
    const bundle =
      bundles.find((b) => b.version === latest.version && (b.status === "success" || b.status === "pending")) ??
      (await updater.download({ url: `${BACKEND}/api/app/${TARGET}/${latest.version}.zip`, version: latest.version }));
    await updater.next({ id: bundle.id });
  } catch (err) {
    console.warn("live update: check failed", err);
  } finally {
    checking = false;
  }
}
