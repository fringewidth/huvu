// huvu's service worker, for huvu added to a phone's home screen
// (src/account/webPush.ts). It only shows notifications. It caches nothing,
// so the page always loads from the host.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// What the backend sends (_web_message in backend/src/huvu/native_push.py):
// title, body, kind, id, and maybe image and opens.
self.addEventListener("push", (event) => {
  if (!event.data) return;
  event.waitUntil(show(event.data.json()));
});

async function show(data) {
  const options = { body: data.body, tag: data.id, data: { kind: data.kind, id: data.id, opens: data.opens ?? null } };
  if (data.image) options.image = data.image;
  await self.registration.showNotification(data.title, options);
  // huvu in front shows it itself, on the stage or in its card. iOS drops
  // a subscription whose pushes show nothing, so the notification shows
  // and is closed at once.
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  if (windows.some((w) => w.visibilityState === "visible")) {
    for (const shown of await self.registration.getNotifications({ tag: data.id })) shown.close();
  }
}

// A tap opens huvu, or brings it to the front, and opens what the notice
// names (src/account/opens.ts).
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(open(event.notification.data ?? {}));
});

async function open(data) {
  const [existing] = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  if (existing) {
    await existing.focus();
    existing.postMessage({ type: "opened", opens: data.opens });
    return;
  }
  const url = new URL(self.registration.scope);
  if (data.opens) url.searchParams.set("opens", data.opens);
  await self.clients.openWindow(url.href);
}
