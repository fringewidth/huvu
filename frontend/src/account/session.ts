import { useSyncExternalStore } from "react";
import { Capacitor } from "@capacitor/core";
import { SecureStorage } from "@aparajita/capacitor-secure-storage";
import * as thread from "../chat/thread";
import { PHONE, TV, ui } from "../config/interaction";
import { call, getJson, getReach, onReach, postJson, setToken, whenUnauthorized, type Reach } from "../net/http";
import * as household from "./household";
import { startNativePush, stopNativePush } from "./nativePush";
import { startPush, stopPush } from "./push";
import { startWebPush, stopWebPush } from "./webPush";
import * as waiting from "./waiting";

export { errorText } from "../net/http";

// Who this device is to the backend. A personal device (phone, web) logs
// in with its user's password. A shared one (the TV) pairs: it shows a
// code, and a member confirms it on their phone. Either way the device
// gets its own token, kept in secure storage: the Android Keystore or the
// iOS Keychain on a phone, and the page's localStorage on web.
export type Role = "member" | "guest";

export interface Person {
  id: string;
  name: string;
}

export interface DeviceInfo {
  id: string;
  name: string;
  kind: string;
  // None on a shared device.
  user: string | null;
  // The group a shared device belongs to. None on a personal device.
  group: string | null;
  created_at: string;
}

export interface Me {
  device: DeviceInfo;
  user: Person | null;
  // Each of the user's groups, with their role there.
  groups: Record<string, Role>;
}

// "booting" while the token is read, "out" with no device token (login,
// or pairing on a shared device), "in" once the stage can show.
export type Status = "booting" | "out" | "in";

interface SessionState {
  status: Status;
  me: Me | null;
  // Why the device is out again, said on the login or pairing screen.
  reason: string | null;
}

const TOKEN_KEY = "huvu.token";

let state: SessionState = { status: "booting", me: null, reason: null };
const listeners = new Set<() => void>();

function set(change: Partial<SessionState>): void {
  state = { ...state, ...change };
  for (const listener of listeners) listener();
}

export async function boot(): Promise<void> {
  whenUnauthorized(() => void signOut("this device was logged out"));
  // Launched while the backend was down, the device isn't known yet. It
  // is loaded once the backend answers again.
  onReach((reach) => {
    if (reach === "up" && state.status === "in" && !state.me) void refreshMe();
  });
  const value = await SecureStorage.getItem(TOKEN_KEY);
  if (value) {
    setToken(value);
    enter();
  } else if (await open()) {
    enter();
  } else {
    set({ status: "out" });
  }
}

// A backend with auth off takes a request with no token as its
// development device, so there is nothing to log in to.
async function open(): Promise<boolean> {
  let answered = false;
  try {
    await call("/api/me", { anonymous: true });
    answered = true;
  } catch {
    // Wants a token, or isn't there: the login screen says which.
  }
  return answered;
}

// The stage shows at once, so a cold start never waits on the network.
// The push stream catches the conversation up once it connects.
function enter(): void {
  set({ status: "in", reason: null });
  startPush();
  if (PHONE) void startNativePush();
  if (PHONE) void startWebPush();
  void refreshMe();
}

export async function refreshMe(): Promise<void> {
  try {
    set({ me: await getJson<Me>("/api/me") });
  } catch (err) {
    console.error("session: could not load this device", err);
  }
}

async function adopt(token: string): Promise<void> {
  await SecureStorage.setItem(TOKEN_KEY, token);
  setToken(token);
  thread.reset();
  enter();
}

export async function login(username: string, password: string, deviceName: string): Promise<void> {
  const { token } = await postJson<{ token: string }>(
    "/api/login",
    { username, password, device: { name: deviceName, kind: ui.deviceKind } },
    { anonymous: true },
  );
  await adopt(token);
}

export async function signOut(reason: string | null): Promise<void> {
  stopPush();
  if (PHONE) void stopNativePush();
  if (PHONE) stopWebPush();
  setToken(null);
  thread.reset();
  waiting.reset();
  household.reset();
  set({ status: "out", me: null, reason });
  await SecureStorage.removeItem(TOKEN_KEY);
}

// Logs this device out on the backend too, so its token is dead even if
// a copy of it survives somewhere.
export async function logOut(): Promise<void> {
  const id = state.me?.device.id;
  if (id) await call(`/api/devices/${id}`, { method: "DELETE" }).catch((err) => console.error("session: could not log out on the backend", err));
  await signOut(null);
}

export interface Pairing {
  pairing: string;
  // As the device shows it: ABCD-EF23.
  code: string;
  expires_at: string;
}

export function startPairing(): Promise<Pairing> {
  return postJson<Pairing>("/api/pair", { name: defaultDeviceName(), kind: ui.deviceKind }, { anonymous: true });
}

// One look at a pairing: false while it waits, true once a member
// confirmed it (the device is in). A pairing that expired throws an
// HttpError with status 410.
export async function checkPairing(pairing: string): Promise<boolean> {
  const res = await call(`/api/pair/${pairing}`, { anonymous: true });
  const body = (await res.json()) as { status: string; token?: string };
  const paired = res.status === 200 && typeof body.token === "string";
  if (paired) await adopt(body.token as string);
  return paired;
}

// A name the user can change on the login screen.
export function defaultDeviceName(): string {
  const agent = navigator.userAgent;
  const browser = /Edg\//.test(agent)
    ? "Edge"
    : /Firefox\//.test(agent)
      ? "Firefox"
      : /Chrome\//.test(agent)
        ? "Chrome"
        : /Safari\//.test(agent)
          ? "Safari"
          : "a browser";
  // An iPhone's browser says "Mac OS X" too, so it is asked first.
  const os = /iPhone/.test(agent)
    ? "iPhone"
    : /Mac OS X/.test(agent)
      ? "Mac"
      : /Windows/.test(agent)
        ? "Windows"
        : /Android/.test(agent)
          ? "Android"
          : /Linux/.test(agent)
            ? "Linux"
            : "";
  const platform = Capacitor.getPlatform();
  const phone = platform === "ios" || os === "iPhone" ? "iPhone" : "phone";
  const web = os ? `${browser} on ${os}` : browser;
  return TV ? "Fire TV" : PHONE ? phone : web;
}

// Whether this user may pair a shared device: from a phone, as a member
// of some group. A guest can't.
export function canPair(me: Me | null): boolean {
  return PHONE && me !== null && Object.values(me.groups).includes("member");
}

// Whether the backend answers, for the stage and the menu to say so.
export function useReach(): Reach {
  return useSyncExternalStore(onReach, getReach);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getSession(): SessionState {
  return state;
}

export function useSession(): SessionState {
  return useSyncExternalStore(subscribe, getSession);
}
