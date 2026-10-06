import * as thread from "../chat/thread";
import { TURN_EVENTS, type TurnEventData } from "../chat/backend";
import { call, HttpError, sse } from "../net/http";
import * as household from "./household";
import * as waiting from "./waiting";

// The push stream: one long-lived `GET /api/push` that carries what the
// backend sends this device unasked. Its own turns (again, and the ones it
// didn't start), notifications, approvals, "is it you?" questions, and who
// is recognized where. It is a fetch, not an EventSource, so the token goes
// in a header and a 401 can be seen.
//
// The backend replays nothing a device missed, so every connect catches up
// first: the conversation, and what waits.

// Waits between tries, doubling up to the last.
const BACKOFF_MS = [1000, 2000, 4000, 8000, 15000, 30000];

let run: AbortController | null = null;
// Cuts a wait between tries short: the app came back to the front.
let wake: (() => void) | null = null;

export function startPush(): void {
  if (run) return;
  const abort = new AbortController();
  run = abort;
  document.addEventListener("visibilitychange", onVisible);
  void loop(abort.signal);
}

export function stopPush(): void {
  run?.abort();
  run = null;
  wake?.();
  document.removeEventListener("visibilitychange", onVisible);
}

// A phone's WebView drops the stream in the background. Back in front, it
// tries again at once instead of waiting out its backoff.
function onVisible(): void {
  if (document.visibilityState === "visible") wake?.();
}

async function loop(signal: AbortSignal): Promise<void> {
  let tries = 0;
  while (!signal.aborted) {
    try {
      const res = await call("/api/push", { signal });
      if (!res.body) throw new Error("push: the backend sent no stream");
      tries = 0;
      void catchUp();
      for await (const { name, data } of sse<Record<string, unknown>>(res.body)) dispatch(name, data);
    } catch (err) {
      // Logged out: the session has taken it from here.
      if (signal.aborted || (err instanceof HttpError && err.status === 401)) return;
      console.error("push: stream lost", err);
    }
    await pause(BACKOFF_MS[Math.min(tries, BACKOFF_MS.length - 1)], signal);
    tries++;
  }
}

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const done = () => {
      window.clearTimeout(timer);
      wake = null;
      resolve();
    };
    const timer = window.setTimeout(done, ms);
    wake = done;
    signal.addEventListener("abort", done, { once: true });
  });
}

async function catchUp(): Promise<void> {
  await Promise.all([thread.sync(), waiting.refresh(), household.refresh()]);
}

function dispatch(name: string, data: Record<string, unknown>): void {
  if (TURN_EVENTS.has(name)) {
    thread.heard((data as unknown as TurnEventData).turn);
  } else if (name === "notify") {
    const notice = data as unknown as thread.NoticeData;
    thread.notify(notice);
    void waiting.markSeen(notice.at);
    // A trigger paused, or firing a lot: the menu's list is out of date.
    if (notice.opens === "triggers") void household.refreshTriggers();
  } else if (name === "approval") {
    waiting.approval(data as unknown as waiting.Approval);
  } else if (name === "identify") {
    waiting.question(data as unknown as waiting.Question);
  } else if (name === "identify.done") {
    waiting.questionDone(String(data.id));
  } else if (name === "attendance") {
    waiting.attendance(data as unknown as Parameters<typeof waiting.attendance>[0]);
  }
}
