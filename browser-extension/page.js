// What a URL keeps before it leaves the browser: no query string and no
// fragment, except the few parameters that say what the page is. The
// backend strips URLs the same way (KEEP in
// backend/src/huvu_drivers/browser.py), so keep the two in step.

// The huvu the extension posts to, unless the options name another.
export const DEFAULT_ADDRESS = "https://huvu.hrishik.me";

const KEEP = {
  "youtube.com/watch": ["v"],
  "youtube.com/results": ["search_query"],
  "google.com/search": ["q"],
  "bing.com/search": ["q"],
  "duckduckgo.com/": ["q"],
};

// The sites that never leave the browser, unless the options say otherwise.
export const DEFAULT_BLOCKED = [
  "mail.google.com",
  "accounts.google.com",
  "outlook.live.com",
  "web.whatsapp.com",
  "localhost",
];

// The URL with only the kept parameters, or null for anything but a web
// page, or a page on a blocked site (or under one: a blocked example.com
// covers mail.example.com).
export function cleanUrl(raw, blocked) {
  let url = null;
  try {
    url = new URL(raw);
  } catch {
    url = null;
  }
  const web = url !== null && (url.protocol === "https:" || url.protocol === "http:");
  const host = web ? url.hostname.replace(/^www\./, "") : "";
  const isBlocked = blocked.some((b) => host === b || host.endsWith(`.${b}`));
  let clean = null;
  if (web && !isBlocked) {
    const site = host.replace(/^m\./, "");
    const keep = KEEP[`${site}${url.pathname || "/"}`] || [];
    const query = new URLSearchParams();
    for (const name of keep) {
      if (url.searchParams.has(name)) query.set(name, url.searchParams.get(name));
    }
    const search = query.toString();
    clean = `${url.protocol}//${url.host}${url.pathname}${search ? `?${search}` : ""}`;
  }
  return clean;
}
