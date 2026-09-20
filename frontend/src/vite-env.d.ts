/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_TARGET: "web" | "phone" | "firetv";
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
