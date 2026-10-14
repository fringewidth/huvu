/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_TARGET: "web" | "phone" | "firetv";
  // The backend's URL under Vite's dev server only, http://localhost:3001
  // by default. Builds talk to their host (net/http.ts).
  readonly VITE_BACKEND_URL?: string;
  // The host's address in the phone's build, from HUVU_PUBLIC_URL
  // (vite.config.ts). Other builds use their page's origin.
  readonly VITE_PUBLIC_URL?: string;
  // "1" builds the garden bench into a production build, talking to the
  // backend on this machine (canvas/bench.ts). Never for a build anyone
  // uses.
  readonly VITE_BENCH?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
