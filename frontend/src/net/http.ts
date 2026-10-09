// The one way to call the backend. Every request carries this device's
// token, and a 401 means the device was logged out: the session hears it
// and sends the user back to login, or the TV back to pairing.
// Every build (web, phone, Fire TV) talks to the host, huvu.hrishik.me,
// which also serves the web build. Only Vite's dev server and a garden
// bench build (VITE_BENCH) read VITE_BACKEND_URL, for a backend on this
// machine.
export const BACKEND = import.meta.env.DEV || import.meta.env.VITE_BENCH === "1"
  ? (import.meta.env.VITE_BACKEND_URL || "http://localhost:3001").replace(/\/$/, "")
  : "https://huvu.hrishik.me";

let token: string | null = null;
let onUnauthorized: () => void = () => {};

export function setToken(next: string | null): void {
  token = next;
}

export function whenUnauthorized(handler: () => void): void {
  onUnauthorized = handler;
}

export function authHeaders(): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// Whether a URL is the backend's own. Only those get the token: a live
// feed's WHEP endpoint can be a media server of the source's own, which
// has no business seeing it.
export function isBackend(url: string): boolean {
  return new URL(url, BACKEND).origin === new URL(BACKEND).origin;
}

// A status other than 2xx. `message` is the backend's own `detail` where
// it sent one, which is safe to show.
export class HttpError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

interface CallInit extends RequestInit {
  // Sent as the JSON body, in place of `body`.
  json?: unknown;
  // Login and pairing answer 401 for a wrong password or code; that says
  // nothing about this device's token.
  anonymous?: boolean;
}

export async function call(path: string, { json, anonymous, headers, ...init }: CallInit = {}): Promise<Response> {
  const res = await fetch(`${BACKEND}${path}`, {
    ...init,
    headers: {
      ...(anonymous ? {} : authHeaders()),
      ...(json === undefined ? {} : { "Content-Type": "application/json" }),
      ...headers,
    },
    body: json === undefined ? init.body : JSON.stringify(json),
  });
  if (res.status === 401 && !anonymous) onUnauthorized();
  if (!res.ok) throw new HttpError(res.status, await detail(res));
  return res;
}

export async function getJson<T>(path: string): Promise<T> {
  return (await call(path)).json() as Promise<T>;
}

export async function postJson<T>(path: string, json: unknown = {}, init: CallInit = {}): Promise<T> {
  return (await call(path, { ...init, method: "POST", json })).json() as Promise<T>;
}

async function detail(res: Response): Promise<string> {
  let message = `the backend answered ${res.status}`;
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") message = body.detail;
  } catch {
    // Not JSON: the status says enough.
  }
  return message;
}

// Server-Sent Events: blocks of `event:` and `data:` lines, ended by a
// blank line. A block with only a comment (a keepalive) says nothing.
export async function* sse<T>(body: ReadableStream<Uint8Array>): AsyncGenerator<{ name: string; data: T }> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    for (let end = buffer.indexOf("\n\n"); end >= 0; end = buffer.indexOf("\n\n")) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let name = "message";
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length > 0) yield { name, data: JSON.parse(data.join("\n")) as T };
    }
  }
}
