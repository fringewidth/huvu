// Every fixed color in huvu. The canvas and components import these
// directly. index.html, the CSS tokens it defines, and the favicon get
// them through the %NAME% placeholders the Vite plugin in vite.config.ts
// fills in. capacitor.config.ts reads PAPER for the native WebView
// background, so the shell never flashes white while the page loads.
// Colors are written as rgb() or hsl(), not hex, so the editor shows a
// swatch and picker on each one.
//
// Colors that a flower draws at random from a range (petals, leaves) stay
// with the flower's own spec, in canvas/flowers/ and canvas/leaves.ts.

// The night-garden page, picked once for the flower/garland palette. The
// rest comes from the garland too: ivory text like moonlit paper, sindoor
// red from the thread for the user's words, marigold from the flower
// cores.
export const PAPER = "rgb(13, 14, 23)";
export const IVORY = "rgb(239, 231, 216)";
export const SINDOOR = "rgb(224, 113, 90)";
export const THREAD = "rgb(156, 58, 46)";
export const MARIGOLD = "rgb(233, 162, 59)";

// The middle of the voice flower's (and favicon's) petal gradient, between
// its marigold base and ivory tips.
export const BLUSH = "rgb(244, 220, 203)";

// Flower cores, keyed by the core type a species names.
export const PETAL_SHADOW = "hsl(280, 15%, 78%)";
export const CORE_DARK = "hsl(20, 70%, 32%)";
export const CORE_YELLOW = "hsl(45, 90%, 60%)";
export const CORE_TINY = "hsl(60, 60%, 70%)";
export const LOTUS_POD = "hsl(48, 90%, 62%)";
export const LOTUS_SEED = "hsl(35, 90%, 45%)";
export const STAMEN_BASE = "hsl(350, 70%, 30%)";
export const POLLEN = "hsl(48, 95%, 60%)";
export const CALYX = "hsl(30, 20%, 35%)";

// The backdrop of the dev seed's stand-in photos.
export const PLATE = "rgb(20, 21, 34)";
