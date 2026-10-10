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

// Whether the backend answered the last call. A network error, or a
// gateway's own error page (the tunnel is up and the backend behind it
// isn't), is "down". Any answer from the backend itself is "up", even an
// error. "unknown" until the first call comes back.
export type Reach = "unknown" | "up" | "down";

let reach: Reach = "unknown";
const reachListeners = new Set<(reach: Reach) => void>();

function setReach(next: Reach): void {
  if (next === reach) return;
  reach = next;
  for (const listener of reachListeners) listener(next);
}

export function getReach(): Reach {
  return reach;
}

export function onReach(listener: (reach: Reach) => void): () => void {
  reachListeners.add(listener);
  return () => reachListeners.delete(listener);
}

export const UNREACHABLE = "huvu isn't answering. Is the backend running?";

// The backend didn't answer: no connection, or a gateway answered for it.
export class Unreachable extends Error {
  constructor() {
    super(UNREACHABLE);
  }
}

// Cloudflare answers 502 or a 52x when the tunnel or the backend behind
// it is down, with a page of its own. The backend's own errors are JSON.
function fromGateway(res: Response): boolean {
  const gatewayStatus = (res.status >= 502 && res.status <= 504) || (res.status >= 520 && res.status <= 530);
  return gatewayStatus && !(res.headers.get("Content-Type") ?? "").includes("json");
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
  let res: Response;
  try {
    res = await fetch(`${BACKEND}${path}`, {
      ...init,
      headers: {
        ...(anonymous ? {} : authHeaders()),
        ...(json === undefined ? {} : { "Content-Type": "application/json" }),
        ...headers,
      },
      body: json === undefined ? init.body : JSON.stringify(json),
    });
  } catch (err) {
    // The caller let go of it. That says nothing about the backend.
    if (init.signal?.aborted) throw err;
    setReach("down");
    throw new Unreachable();
  }
  if (fromGateway(res)) {
    setReach("down");
    throw new Unreachable();
  }
  setReach("up");
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

// What to say when a call failed: the backend's own words, or that it
// didn't answer at all.
export function errorText(err: unknown): string {
  return err instanceof HttpError ? err.message : UNREACHABLE;
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
