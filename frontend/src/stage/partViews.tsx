import { useEffect, useRef } from "react";
import { ui } from "../config/interaction";
import { capsClass, NUMERALS } from "./style";
import { useWhepFeed } from "./useWhepFeed";
import type { ToolPart } from "../chat/thread";

// The hero views of the tool parts listed in parts.ts.

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

export function WebSearchNote({ part }: { part: ToolPart }) {
  const query = str(part.args.query);
  const sources = (part.result as { sources?: unknown } | undefined)?.sources;
  const count = Array.isArray(sources) ? sources.length : null;
  return (
    <p className={`${capsClass} halo text-ivory/50`}>
      searched the web{query && ` / ${query}`}
      {count !== null && ` / ${count} sources`}
    </p>
  );
}

// huvu at work in its browser. Its screen can be watched from a device of
// the person's own, which a screen with no input of its own isn't.
export function BrowserNote({ onWatch }: { part: ToolPart; onWatch: () => void }) {
  return (
    <p className={`${capsClass} halo flex items-baseline gap-2 text-ivory/50`}>
      used the browser
      {ui.hasInput && (
        <button
          type="button"
          onClick={onWatch}
          className={`${capsClass} -my-3 min-h-11 px-2 text-ivory/70 outline-none transition-colors duration-500 hover:text-ivory focus-visible:ring-1 focus-visible:ring-sindoor/60`}
        >
          watch
        </button>
      )}
    </p>
  );
}

// A live video feed from huvu's side: its browser, a camera, a screen.
// `src` is a WHEP endpoint (see useWhepFeed). `label` names the source,
// `caption` says what it shows right now. Only the stage's turn shows its
// feed, so a turn that folds away hangs up. When the feed ends, its last
// frame stays.
export function VideoFeed({ part }: { part: ToolPart }) {
  const src = str(part.args.src);
  const label = str(part.args.label) || "live feed";
  const caption = str(part.args.caption);
  const { stream, state } = useWhepFeed(src);
  const video = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    if (video.current) video.current.srcObject = stream;
  }, [stream]);
  const waiting = !src ? "the feed streams here" : state === "lost" ? "the feed stopped" : "the feed is connecting";
  return (
    <figure className="m-0 flex max-w-[36rem] flex-col gap-2">
      <figcaption className={`${capsClass} flex min-w-0 items-center gap-3 text-ivory/60`}>
        <span className={`size-1.5 shrink-0 rounded-full transition-colors duration-700 ${state === "live" ? "bg-sindoor" : "bg-ivory/30"}`} />
        <span className="shrink-0">{state === "lost" ? "ended" : "live"} / {label}</span>
        {caption && <span className="truncate normal-case tracking-[0.08em] text-ivory/40">{caption}</span>}
      </figcaption>
      <div className="hatch relative flex aspect-video w-full items-center justify-center border border-ivory/20 bg-paper/80">
        <span className={`font-bodoni italic text-ivory/45 transition-opacity duration-700 ${stream ? "opacity-0" : "opacity-100"}`}>{waiting}</span>
        <video
          ref={video}
          autoPlay
          muted
          playsInline
          aria-label={caption ? `${label}: ${caption}` : label}
          className={`absolute inset-0 size-full object-cover transition-opacity duration-700 ${stream ? "opacity-100" : "opacity-0"}`}
        />
      </div>
    </figure>
  );
}

export function Choices({ part, onChoose }: { part: ToolPart; onChoose: (text: string) => void }) {
  const raw = part.args.options;
  const options = Array.isArray(raw) ? raw.filter((o): o is string => typeof o === "string") : [];
  return (
    <div className="flex flex-wrap gap-x-8">
      {options.map((option, i) => (
        <button
          key={option}
          type="button"
          data-choice=""
          onClick={() => onChoose(option)}
          className="flex min-h-11 items-baseline gap-2 font-bodoni text-[19px] text-ivory transition-colors duration-500 hover:text-sindoor focus-visible:text-sindoor focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-sindoor/50 in-data-[target=firetv]:focus-visible:ring-2 sm:text-[21px]"
        >
          <span className={`${capsClass} text-sindoor`}>{NUMERALS[i] ?? `${i + 1}.`}</span>
          {option}
        </button>
      ))}
    </div>
  );
}

export function GenericNote({ part }: { part: ToolPart }) {
  return <p className={`${capsClass} halo text-ivory/50`}>used {part.toolName.replace(/[_-]+/g, " ")}</p>;
}
