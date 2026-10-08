/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_TARGET: "web" | "phone" | "firetv";
  // The backend's URL. Defaults to http://localhost:3001; a phone reaches
  // that over USB with `adb reverse tcp:3001 tcp:3001`.
  readonly VITE_BACKEND_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
