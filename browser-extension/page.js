// Which visits leave the browser. Only a visit's title is ever sent; its
// address is read here, to skip what isn't a web page and the sites kept
// out, and goes nowhere.

// The sites that never leave the browser, unless the options say otherwise.
export const DEFAULT_BLOCKED = [
  "mail.google.com",
  "accounts.google.com",
  "outlook.live.com",
  "web.whatsapp.com",
  "localhost",
];

// Whether a visit to this address may be sent: a web page, on no blocked
// site (a blocked example.com covers mail.example.com too).
export function sendable(raw, blocked) {
  let url = null;
  try {
    url = new URL(raw);
  } catch {
    url = null;
  }
  const web = url !== null && (url.protocol === "https:" || url.protocol === "http:");
  const host = web ? url.hostname.replace(/^www\./, "") : "";
  return web && !blocked.some((b) => host === b || host.endsWith(`.${b}`));
}
