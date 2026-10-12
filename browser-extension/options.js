import { DEFAULT_ADDRESS, DEFAULT_BLOCKED } from "./page.js";
import { current } from "./pause.js";

const $ = (id) => document.getElementById(id);

async function load() {
  const s = await chrome.storage.local.get(["address", "token", "blocked"]);
  $("address").value = s.address || DEFAULT_ADDRESS;
  $("token").value = s.token || "";
  $("blocked").value = (s.blocked ?? DEFAULT_BLOCKED).join("\n");
  await showStatus();
}

async function save() {
  const address = $("address").value.trim().replace(/\/+$/, "") || DEFAULT_ADDRESS;
  // Another huvu than the default needs Chrome's leave to reach it.
  if (address !== DEFAULT_ADDRESS) {
    const granted = await chrome.permissions.request({ origins: [`${new URL(address).origin}/*`] });
    if (!granted) {
      $("status").textContent = `Chrome didn't allow posting to ${address}.`;
      return;
    }
  }
  const blocked = $("blocked")
    .value.split("\n")
    .map((line) => line.trim().replace(/^www\./, ""))
    .filter(Boolean);
  await chrome.storage.local.set({ address, token: $("token").value.trim(), blocked });
  await sendNow();
}

async function sendNow() {
  $("status").textContent = "Sending.";
  await chrome.runtime.sendMessage("sync");
  await showStatus();
}

async function showStatus() {
  const { lastSync, lastError, sent = 0, cursor, pauses = [] } = await chrome.storage.local.get(["lastSync", "lastError", "sent", "cursor", "pauses"]);
  const lines = [`${sent.toLocaleString()} visits sent.`];
  const on = current(pauses);
  if (on) lines.push(on.until === null ? "Paused until you resume." : `Paused until ${new Date(on.until).toLocaleString()}.`);
  if (cursor) lines.push(`Sent up to ${new Date(cursor).toLocaleString()}.`);
  if (lastSync) lines.push(`Last sync ${new Date(lastSync).toLocaleString()}.`);
  $("status").textContent = lines.join("\n");
  if (lastError) {
    const error = document.createElement("div");
    error.className = "error";
    error.textContent = `Last error ${lastError}`;
    $("status").append(error);
  }
}

$("save").addEventListener("click", () => void save());
$("now").addEventListener("click", () => void sendNow());
void load();
