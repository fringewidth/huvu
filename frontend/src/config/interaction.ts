export type Target = "web" | "phone" | "firetv";
export type Mode = "text" | "voice";

const VALID_TARGETS: Target[] = ["web", "phone", "firetv"];

function resolveTarget(): Target {
  const raw = import.meta.env.VITE_TARGET;
  if (!VALID_TARGETS.includes(raw as Target)) {
    throw new Error(
      `VITE_TARGET must be set to one of ${VALID_TARGETS.join(", ")}, got ${JSON.stringify(raw)}`,
    );
  }
  return raw;
}

export const TARGET: Target = resolveTarget();

export const defaultMode: Record<Target, Mode> = {
  web: "text",
  phone: "text",
  firetv: "voice",
};

export const modeSwitchable: Record<Target, boolean> = {
  web: true,
  phone: true,
  firetv: false,
};

export function initialMode(): Mode {
  if (typeof window !== "undefined" && modeSwitchable[TARGET]) {
    const requested = new URLSearchParams(window.location.search).get("mode");
    if (requested === "text" || requested === "voice") return requested;
  }
  return defaultMode[TARGET];
}
