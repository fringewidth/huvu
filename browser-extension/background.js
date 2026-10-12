// Reads Chrome's own history about once a minute and posts the visits huvu
// hasn't had yet to POST /api/ingest/browser (backend/src/huvu_drivers/
// browser.py). The cursor is the time of the last visit huvu took: it moves
// only after a post succeeds, so a visit made while huvu is down goes later.
// The first sync sends the 90 days Chrome keeps.
//
// The history is read, not watched with history.onVisited: a page has no
// title yet when the visit happens, and a sync a minute later finds both.

import { cleanUrl, DEFAULT_ADDRESS, DEFAULT_BLOCKED } from "./page.js";

const KEPT_MS = 90 * 24 * 60 * 60 * 1000;
// A visit younger than this waits for the next sync, so its page has a title.
const SETTLE_MS = 30 * 1000;
// Visits per post; the backend takes at most 1000.
const BATCH = 500;
const ALARM = "sync";

chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(schedule);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) void sync();
});
chrome.action.onClicked.addListener(() => void chrome.runtime.openOptionsPage());
// The options page asks for a sync after a save, and with "send now".
chrome.runtime.onMessage.addListener((message, _sender, reply) => {
  if (message === "sync") {
    void sync().then(() => reply(true));
    return true;
  }
  return false;
});

function schedule() {
  void chrome.alarms.create(ALARM, { periodInMinutes: 1 });
  void sync();
}

// One sync at a time: an alarm during a long first sync joins it.
let running = null;
function sync() {
  running ??= run().finally(() => {
    running = null;
  });
  return running;
}

async function run() {
  const settings = await chrome.storage.local.get(["address", "token", "blocked", "cursor"]);
  const until = Date.now() - SETTLE_MS;
  const cursor = settings.cursor ?? until - KEPT_MS;
  try {
    if (!settings.token) throw new Error("No token yet: set one in the options.");
    const visits = await visitsBetween(cursor, until, settings.blocked ?? DEFAULT_BLOCKED);
    for (let i = 0; i < visits.length; i += BATCH) {
      const batch = visits.slice(i, i + BATCH);
      await post(settings.address || DEFAULT_ADDRESS, settings.token, batch);
      await chrome.storage.local.set({ cursor: batch[batch.length - 1].at });
      await count(batch.length);
    }
    await chrome.storage.local.set({ cursor: until, lastSync: Date.now(), lastError: null });
  } catch (err) {
    await chrome.storage.local.set({ lastError: `${new Date().toLocaleString()}: ${err.message ?? err}` });
  }
}

// Every visit from `from` to `until`, oldest first. A visit at the cursor's
// own time is sent again; the backend keeps it once.
async function visitsBetween(from, until, blocked) {
  const items = await chrome.history.search({ text: "", startTime: from, maxResults: 100000 });
  const visits = [];
  for (const item of items) {
    const url = cleanUrl(item.url, blocked);
    if (url === null) continue;
    for (const visit of await chrome.history.getVisits({ url: item.url })) {
      if (visit.visitTime >= from && visit.visitTime <= until) {
        visits.push({ id: visit.visitId, url, title: item.title || null, at: Math.round(visit.visitTime), transition: visit.transition });
      }
    }
  }
  return visits.sort((a, b) => a.at - b.at);
}

async function post(address, token, visits) {
  const res = await fetch(`${address}/api/ingest/browser`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ visits }),
  });
  if (!res.ok) throw new Error(`huvu answered ${res.status}: ${(await res.text()).slice(0, 200)}`);
}

async function count(n) {
  const { sent = 0 } = await chrome.storage.local.get("sent");
  await chrome.storage.local.set({ sent: sent + n });
}
