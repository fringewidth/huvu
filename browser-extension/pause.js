// Pausing. A pause is a window of time, and no visit inside it ever leaves
// the browser. A window with no end lasts until the person resumes. A window
// stays in storage until the sync cursor has passed its end, since the sync
// reads each visit about a minute after it happens.

// Whether the pause covers the moment `at`.
function covers(pause, at) {
  return pause.from <= at && (pause.until === null || at < pause.until);
}

// Whether the visit at `at` falls in a pause.
export function paused(at, pauses) {
  return pauses.some((p) => covers(p, at));
}

// The pause on now, or null.
export function current(pauses, now = Date.now()) {
  return pauses.find((p) => covers(p, now)) ?? null;
}

export async function pauses() {
  const { pauses = [] } = await chrome.storage.local.get("pauses");
  return pauses;
}

// Pauses for `minutes` from now, or until resume with null. A pause on now
// gets the new end.
export async function pause(minutes) {
  const now = Date.now();
  const until = minutes === null ? null : now + minutes * 60 * 1000;
  const all = await pauses();
  const on = current(all, now);
  if (on) {
    on.until = until;
  } else {
    all.push({ from: now, until });
  }
  await save(all);
}

export async function resume() {
  const now = Date.now();
  const all = await pauses();
  for (const p of all) {
    if (covers(p, now)) p.until = now;
  }
  await save(all);
}

// Drops the windows the cursor has passed. Only the popup writes pauses, so
// a sync can't overwrite one made while it runs.
async function save(all) {
  const { cursor = 0 } = await chrome.storage.local.get("cursor");
  await chrome.storage.local.set({ pauses: all.filter((p) => p.until === null || p.until > cursor) });
}
