import { capsClass } from "../stage/style";
import type { WikiPage } from "../wiki/wiki";

export const buttonClass = `${capsClass} -mx-2 min-h-11 shrink-0 px-2 outline-none transition-colors duration-500 focus-visible:ring-1 focus-visible:ring-sindoor/60`;

// Where the wiki is: its contents, a page being read, or a page being
// edited. A new page is an edit with no version yet.
export type Place =
  | { kind: "contents" }
  | { kind: "page"; page: WikiPage }
  | { kind: "edit"; page: WikiPage | null; path: string; draft: string };
