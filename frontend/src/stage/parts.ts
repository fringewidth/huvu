import type { FC } from "react";
import { BrowserNote, Choices, GenericNote, LearnVoice, VideoFeed, WebSearchNote, YouTubeVideo } from "./partViews";
import type { RichPart, ToolPart } from "../chat/thread";

// Everything in a turn that isn't plain text: images, tool calls, and
// whatever ad hoc thing a tool wants to show tomorrow. Each tool name maps
// to a chip (what a collapsed history row shows) and a hero view (what the
// stage shows when the turn is big). A tool without an entry here still
// gets a chip and a one-line note, so a new tool never breaks the stage.

// Where a part sits in the hero: notes inside the answer, where huvu ran
// them, panels below it, choices last.
export type Slot = "note" | "panel" | "choices";

interface ToolKind {
  chip: string;
  slot: Slot;
  Hero: FC<{ part: ToolPart; onChoose: (text: string) => void; onWatch: () => void }>;
}

const TOOL_KINDS: Record<string, ToolKind> = {
  web_search: { chip: "web", slot: "note", Hero: WebSearchNote },
  video_feed: { chip: "live", slot: "panel", Hero: VideoFeed },
  youtube: { chip: "youtube", slot: "panel", Hero: YouTubeVideo },
  learn_voice: { chip: "your voice", slot: "panel", Hero: LearnVoice },
  choices: { chip: "choices", slot: "choices", Hero: Choices },
};

// Hermes's browser tools are all steps of the same thing, so they share a
// kind, and a run of them shows as one note.
const BROWSER: ToolKind = { chip: "browser", slot: "note", Hero: BrowserNote };

export function isBrowser(part: ToolPart): boolean {
  return part.toolName.startsWith("browser_");
}

export function kindOf(toolName: string): ToolKind {
  if (toolName.startsWith("browser_")) return BROWSER;
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

// A piece of the answer, or the notes that came between two pieces.
export type Piece = { text: string } | { notes: RichPart[] };

// The answer cut where each note came, so the stage shows what huvu said
// and did in the order it happened.
export function interleave(answer: string, notes: RichPart[]): Piece[] {
  const pieces: Piece[] = [];
  let from = 0;
  for (const part of [...notes].sort((a, b) => (a.after ?? 0) - (b.after ?? 0))) {
    const cut = Math.max(from, Math.min(part.after ?? 0, answer.length));
    const text = answer.slice(from, cut).trim();
    if (text) pieces.push({ text });
    from = cut;
    const last = pieces.at(-1);
    if (last && "notes" in last) last.notes.push(part);
    else pieces.push({ notes: [part] });
  }
  const rest = answer.slice(from).trim();
  if (rest) pieces.push({ text: rest });
  return pieces;
}
