// What a tapped notification opens. A tap only opens the app: approvals
// and questions wait in their card, and an unseen notice takes the stage.
// A notice that names a screen (`opens`) opens it. Native push
// (nativePush.ts) and Web Push (webPush.ts) both land here.

// The screens a notice can open. The backend names them in `opens`.
export type Opens = "triggers";

let opener: ((opens: Opens) => void) | null = null;
// A tap that came before anyone listened: a cold start from the tray.
let pending: Opens | null = fromUrl();

// The stage opens what a tapped notice names. Returns the unsubscribe.
export function onOpens(handler: (opens: Opens) => void): () => void {
  opener = handler;
  if (pending) handler(pending);
  pending = null;
  return () => {
    if (opener === handler) opener = null;
  };
}

export function opened(data: unknown): void {
  const opens = (data as { opens?: unknown } | undefined)?.opens;
  if (opens !== "triggers") return;
  if (opener) opener(opens);
  else pending = opens;
}

// A home screen web app that a tap started opens at `/?opens=...`
// (public/sw.js). The address is put back, so a reload opens nothing.
function fromUrl(): Opens | null {
  const url = new URL(window.location.href);
  const opens = url.searchParams.get("opens");
  if (opens === null) return null;
  url.searchParams.delete("opens");
  window.history.replaceState(null, "", url);
  return opens === "triggers" ? opens : null;
}
