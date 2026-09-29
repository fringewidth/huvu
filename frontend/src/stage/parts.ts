import type { FC } from "react";
import type { RichPart } from "./turns";
import { BrowserPanel, Choices, GenericNote, WebSearchNote } from "./partViews";

// Everything in a turn that isn't plain text: images, tool calls, and
// whatever ad hoc thing a tool wants to show tomorrow. Each tool name maps
// to a chip (what a collapsed history row shows) and a hero view (what the
// stage shows when the turn is big). A tool without an entry here still
// gets a chip and a one-line note, so a new tool never breaks the stage.
export type ToolPart = Extract<RichPart, { kind: "tool" }>;
export type ImagePart = Extract<RichPart, { kind: "image" }>;

// Where a part sits in the hero: notes above the answer, panels below it,
// choices last.
export type Slot = "note" | "panel" | "choices";

interface ToolKind {
  chip: string;
  slot: Slot;
  Hero: FC<{ part: ToolPart; onChoose: (text: string) => void }>;
}

const TOOL_KINDS: Record<string, ToolKind> = {
  web_search: { chip: "web", slot: "note", Hero: WebSearchNote },
  browser: { chip: "browser", slot: "panel", Hero: BrowserPanel },
  choices: { chip: "choices", slot: "choices", Hero: Choices },
};

export function kindOf(toolName: string): ToolKind {
  return TOOL_KINDS[toolName] ?? { chip: toolName.replace(/[_-]+/g, " "), slot: "note", Hero: GenericNote };
}

export function chipsFor(parts: RichPart[]): string[] {
  const images = parts.filter((p) => p.kind === "image").length;
  const chips = new Set(parts.flatMap((p) => (p.kind === "tool" ? [kindOf(p.toolName).chip] : [])));
  return [...(images ? [images === 1 ? "1 image" : `${images} images`] : []), ...chips];
}

export function partsInSlot(parts: RichPart[], slot: Slot): RichPart[] {
  return parts.filter((p) => (p.kind === "image" ? slot === "panel" : kindOf(p.toolName).slot === slot));
}
