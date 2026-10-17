import { useSyncExternalStore } from "react";
import { SHARED } from "../config/interaction";
import { call, errorText, getJson, postJson } from "../net/http";
import type { DeviceInfo, Person } from "./session";

// What the menu under the wordmark lists: the user's triggers, their
// devices and their groups' shared ones, and on a shared device the
// people who may be recognized at it.
export interface Trigger {
  id: string;
  note: string;
  // edge: runs when its query's answer turns up; rows: for each new row.
  fire: "edge" | "rows";
  // Ends after its first fire.
  once: boolean;
  // `awaiting`: an advanced trigger waiting for its person's yes.
  status: "active" | "unconfirmed" | "awaiting" | "paused";
  // The model its wakes run on: basic, pro or advanced.
  tier: "basic" | "pro" | "advanced";
  // Who paused it. One a limit paused (`limit`: it fired or woke huvu too
  // often) comes back only by a person's tap, here.
  paused_by: "model" | "user" | "limit" | null;
  // How many of its next fires it skips, running nothing.
  skip: number;
  last_fired_at: string | null;
  fire_count: number;
  expires_at: string | null;
}

// A list is null until it first loads: huvu hasn't said, so nothing can
// be said about it. A failed load keeps the last list, and says why.
interface HouseholdState {
  triggers: Trigger[] | null;
  devices: DeviceInfo[] | null;
  people: Person[];
  trouble: string | null;
}

const EMPTY: HouseholdState = { triggers: null, devices: null, people: [], trouble: null };

let state: HouseholdState = EMPTY;
const listeners = new Set<() => void>();

function set(change: Partial<HouseholdState>): void {
  state = { ...state, ...change };
  for (const listener of listeners) listener();
}

export async function refreshTriggers(): Promise<void> {
  try {
    set({ triggers: await getJson<Trigger[]>("/api/triggers"), trouble: null });
  } catch (err) {
    console.error("household: could not load the triggers", err);
    set({ trouble: errorText(err) });
  }
}

export async function setPaused(id: string, paused: boolean): Promise<void> {
  const trigger = await postJson<Trigger>(`/api/triggers/${id}/${paused ? "pause" : "resume"}`);
  set({ triggers: state.triggers?.map((t) => (t.id === id ? trigger : t)) ?? null });
}

// Has a trigger skip its next fire, or skip none.
export async function setSkipping(id: string, skipping: boolean): Promise<void> {
  const trigger = await postJson<Trigger>(`/api/triggers/${id}/${skipping ? "skip" : "unskip"}`);
  set({ triggers: state.triggers?.map((t) => (t.id === id ? trigger : t)) ?? null });
}

// Says yes to an advanced trigger. On the person's own device the tap is
// the yes, and the trigger comes back live.
export async function allow(id: string): Promise<void> {
  const trigger = await postJson<Trigger>(`/api/triggers/${id}/approve`);
  set({ triggers: state.triggers?.map((t) => (t.id === id ? trigger : t)) ?? null });
}

export async function refreshDevices(): Promise<void> {
  try {
    set({ devices: await getJson<DeviceInfo[]>("/api/devices"), trouble: null });
  } catch (err) {
    console.error("household: could not load the devices", err);
    set({ trouble: errorText(err) });
  }
}

// Logs another device out. This device logs out through the session.
export async function revoke(id: string): Promise<void> {
  await call(`/api/devices/${id}`, { method: "DELETE" });
  set({ devices: state.devices?.filter((d) => d.id !== id) ?? null });
}

export async function refreshPeople(): Promise<void> {
  try {
    set({ people: await getJson<Person[]>("/api/people") });
  } catch (err) {
    console.error("household: could not load the people", err);
  }
}

// A phone confirms the code a shared device shows. `group` is needed only
// when the user is a member of more than one.
export async function confirmPairing(code: string, group: string | null): Promise<DeviceInfo> {
  const device = await postJson<DeviceInfo>("/api/pair/confirm", { code, ...(group ? { group } : {}) });
  void refreshDevices();
  return device;
}

// What the push stream's catch-up loads, so the wordmark can say a trigger
// needs a tap before the menu opens.
export function refresh(): Promise<void> {
  return SHARED ? refreshPeople() : refreshTriggers();
}

export function reset(): void {
  set(EMPTY);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getHousehold(): HouseholdState {
  return state;
}

export function useHousehold(): HouseholdState {
  return useSyncExternalStore(subscribe, getHousehold);
}
