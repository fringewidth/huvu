// The toolbar popup: pause sending, with or without a timer, and resume.

import { current, pause, pauses, resume } from "./pause.js";

const $ = (id) => document.getElementById(id);

async function show() {
  const on = current(await pauses());
  $("resume").hidden = on === null;
  $("state").textContent = describe(on);
}

function describe(on) {
  if (on === null) return "Sending the titles of the pages you open.";
  if (on.until === null) return "Paused. Nothing you open is sent until you resume.";
  const time = new Date(on.until).toLocaleTimeString([], { timeStyle: "short" });
  return `Paused until ${time}. Nothing you open until then is sent.`;
}

for (const button of document.querySelectorAll("[data-minutes]")) {
  button.addEventListener("click", async () => {
    const minutes = button.dataset.minutes === "" ? null : Number(button.dataset.minutes);
    await pause(minutes);
    await show();
  });
}
$("resume").addEventListener("click", async () => {
  await resume();
  await show();
});
$("settings").addEventListener("click", (event) => {
  event.preventDefault();
  void chrome.runtime.openOptionsPage();
  window.close();
});
void show();
