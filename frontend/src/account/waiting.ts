import { useSyncExternalStore } from "react";
import { Preferences } from "@capacitor/preferences";
import * as thread from "../chat/thread";
import { PHONE, SHARED } from "../config/interaction";
import { call, getJson, HttpError, postJson } from "../net/http";
import type { Person } from "./session";

// What waits on a person, and who is where. A phone answers approvals and
// "is it you?" questions, and knows the shared devices its user is
// recognized at, so it can end that from afar. A shared device knows who
// is recognized at it, and whom it asked. The push stream keeps all of it
// current; refresh() catches up whenever the stream connects, since the
// backend replays nothing a device missed.
export interface Approval {
  id: string;
  // `source.action`, and in words.
  action: string;
  label: string;
  args: Record<string, unknown>;
  reason: string | null;
  // Who asked, in words: "huvu, talking with Me on laptop", "a trigger:
  // ...".
  requester: string;
  // The approval belongs to this user, or, when null, to the members of
  // `group`; then the first answer wins.
  user: string | null;
  group: string | null;
  status: string;
  created_at: string;
  deadline: string;
}

export interface Question {
  id: string;
  device: { id: string; name: string };
  user: string;
  // device, model, voice or trigger.
  asked_by: string;
  // What set it off, such as the trigger being set up.
  reason: string | null;
  asked_at: string;
  expires_at: string;
}

export interface Place {
  device: { id: string; name: string };
}

interface WaitingState {
  approvals: Approval[];
  questions: Question[];
  // A shared device: who is recognized at it.
  here: Person | null;
  // A shared device: the person it asked, until they answer on their
  // phone or the question runs out.
  asking: { person: Person; until: number } | null;
  // A phone: the shared devices its user is recognized at.
  places: Place[];
}

const EMPTY: WaitingState = { approvals: [], questions: [], here: null, asking: null, places: [] };
// How long a phone has to answer "is it you?".
const ASK_MS = 5 * 60_000;
// The newest notification already shown, so the ones sent while the app
// was closed can take the stage once.
const SEEN_KEY = "huvu.noticesSeen";

let state: WaitingState = EMPTY;
let askTimer = 0;
const listeners = new Set<() => void>();

function set(change: Partial<WaitingState>): void {
  state = { ...state, ...change };
  for (const listener of listeners) listener();
}

export async function refresh(): Promise<void> {
  try {
    if (PHONE) await refreshPhone();
    if (SHARED) set({ here: (await getJson<{ user: Person | null }>("/api/attendance")).user });
  } catch (err) {
    console.error("waiting: could not catch up", err);
  }
}

async function refreshPhone(): Promise<void> {
  const [approvals, questions, places, sent, seen] = await Promise.all([
    getJson<Approval[]>("/api/approvals"),
    getJson<Question[]>("/api/identify"),
    getJson<Place[]>("/api/attendance/mine"),
    getJson<thread.NoticeData[]>("/api/notifications"),
    Preferences.get({ key: SEEN_KEY }),
  ]);
  set({ approvals, questions, places });
  thread.notices(sent, seen.value ? Number(seen.value) : null);
  await markSeen(sent.at(-1)?.at);
}

export async function markSeen(at: string | undefined): Promise<void> {
  if (at) await Preferences.set({ key: SEEN_KEY, value: String(Date.parse(at)) });
}

// From the push stream.

export function approval(a: Approval): void {
  const others = state.approvals.filter((x) => x.id !== a.id);
  // A resolved one was answered, by this phone or another, or ran out.
  set({ approvals: a.status === "pending" ? [...others, a] : others });
}

export function question(q: Question): void {
  set({ questions: [...state.questions.filter((x) => x.id !== q.id), q] });
}

// Answered, run out, or withdrawn since someone else said yes first.
export function questionDone(id: string): void {
  set({ questions: state.questions.filter((x) => x.id !== id) });
}

export function attendance(change: { device?: { id: string; name: string }; user: Person | null }): void {
  if (SHARED) {
    if (change.user) window.clearTimeout(askTimer);
    set({ here: change.user, asking: change.user ? null : state.asking });
  } else if (change.device) {
    const device = change.device;
    const others = state.places.filter((p) => p.device.id !== device.id);
    set({ places: change.user ? [...others, { device }] : others });
  }
}

// Answers. A 409 or 410 means it was answered already (a double tap, or
// another phone first) or ran out; it goes quietly.

export async function answerApproval(id: string, approve: boolean): Promise<void> {
  try {
    await postJson(`/api/approvals/${id}`, { approve });
  } catch (err) {
    if (!(err instanceof HttpError && (err.status === 409 || err.status === 410))) throw err;
  }
  set({ approvals: state.approvals.filter((x) => x.id !== id) });
}

export async function answerQuestion(id: string, yes: boolean): Promise<void> {
  try {
    await postJson(`/api/identify/${id}`, { yes });
  } catch (err) {
    if (!(err instanceof HttpError && [404, 409, 410].includes(err.status))) throw err;
  }
  questionDone(id);
}

// A shared device asks a person, on their phone, whether it's them here.
// Asking again before they answer changes nothing (409).
export async function askWho(person: Person): Promise<void> {
  try {
    await postJson("/api/attendance", { user: person.id });
  } catch (err) {
    if (!(err instanceof HttpError && err.status === 409)) throw err;
  }
  window.clearTimeout(askTimer);
  askTimer = window.setTimeout(() => set({ asking: null }), ASK_MS);
  set({ asking: { person, until: Date.now() + ASK_MS } });
}

// Ends who is recognized at a device: this shared device itself, or one
// this phone's user is recognized at.
export async function release(deviceId: string): Promise<void> {
  await call(`/api/attendance/${deviceId}`, { method: "DELETE" });
  attendance({ device: { id: deviceId, name: "" }, user: null });
}

export function reset(): void {
  window.clearTimeout(askTimer);
  set(EMPTY);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getWaiting(): WaitingState {
  return state;
}

export function useWaiting(): WaitingState {
  return useSyncExternalStore(subscribe, getWaiting);
}
