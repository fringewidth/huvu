import { useSyncExternalStore } from "react";
import { call, getJson } from "../net/http";
import * as thread from "./thread";

// Wakes: model instances a trigger started for this user. Each is a
// conversation of its own. The menu lists the open ones, running or
// waiting for the user, and a tapped notice from one opens it. On the
// stage, what the user sends goes to the wake, and huvu carries on its
// work there. A wake ends when its run does, unless the model asked to
// wait; the user can end it too.

export interface WakeInfo {
  id: string;
  trigger: string | null;
  // The trigger's note, which names the wake.
  note: string;
  tier: string;
  status: "running" | "waiting" | "ended";
  // What it waits for from the user, while it waits.
  waits_for: string | null;
  started_at: string;
  ran_at: string | null;
  // When the wait ends by itself.
  until: string | null;
  // Whether what the user sends continues it: while it waits, and for a
  // day after a run that ended it, unless they ended it themselves.
  answerable: boolean;
}

interface WakesState {
  // The open wakes, newest first.
  list: WakeInfo[];
  // The wake on the stage, open or ended. Null shows the device's own
  // conversation.
  shown: WakeInfo | null;
}

let state: WakesState = { list: [], shown: null };
const listeners = new Set<() => void>();

function set(next: Partial<WakesState>): void {
  state = { ...state, ...next };
  for (const l of listeners) l();
}

export async function refreshWakes(): Promise<void> {
  try {
    set({ list: await getJson<WakeInfo[]>("/api/wakes") });
  } catch (err) {
    console.error("wakes: could not load them", err);
  }
}

// A wake changed (`wake` on the push stream): it started, waits, runs
// again or ended.
export function wakeChanged(wake: WakeInfo): void {
  const others = state.list.filter((w) => w.id !== wake.id);
  const list = wake.status === "ended" ? others : [wake, ...others].sort((a, b) => b.started_at.localeCompare(a.started_at));
  set({ list, shown: state.shown?.id === wake.id ? wake : state.shown });
}

// Puts a wake on the stage.
export async function showWake(id: string): Promise<void> {
  const listed = state.list.find((w) => w.id === id) ?? null;
  set({ shown: listed ?? { id, trigger: null, note: "", tier: "", status: "running", waits_for: null, started_at: "", ran_at: null, until: null, answerable: true } });
  void thread.showWake(id);
  try {
    const { wake } = await getJson<{ wake: WakeInfo }>(`/api/wakes/${id}`);
    if (state.shown?.id === id) set({ shown: wake });
  } catch (err) {
    console.error("wakes: could not open it", err);
  }
}

// Back to the device's own conversation.
export function leaveWake(): void {
  if (!state.shown) return;
  set({ shown: null });
  thread.showOwn();
}

// The user ends a wake, and huvu stops what it was doing there.
export async function endWake(id: string): Promise<void> {
  await call(`/api/wakes/${id}/close`, { method: "POST" });
}

export function isWakeShown(): boolean {
  return state.shown !== null;
}

export function resetWakes(): void {
  set({ list: [], shown: null });
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getWakes(): WakesState {
  return state;
}

export function useWakes(): WakesState {
  return useSyncExternalStore(subscribe, getWakes);
}
