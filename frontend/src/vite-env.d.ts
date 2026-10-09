/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_TARGET: "web" | "phone" | "firetv";
  // The backend's URL under Vite's dev server only, http://localhost:3001
  // by default. Builds always use https://huvu.hrishik.me (net/http.ts).
  readonly VITE_BACKEND_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
