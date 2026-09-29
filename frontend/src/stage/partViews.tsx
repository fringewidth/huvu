import type { RichPart } from "./turns";
import { capsClass } from "./style";

// The hero views of the tool parts listed in parts.ts.
type ToolPart = Extract<RichPart, { kind: "tool" }>;

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

export function BrowserPanel({ part }: { part: ToolPart }) {
  const url = str(part.args.url);
  const shot = str((part.result as { screenshot?: unknown } | undefined)?.screenshot);
  return (
    <figure className="m-0 flex max-w-[36rem] flex-col gap-2">
      <figcaption className={`${capsClass} flex min-w-0 items-center gap-3 text-ivory/60`}>
        <span className="size-1.5 shrink-0 rounded-full bg-sindoor" />
        <span className="shrink-0">live / huvu's browser</span>
        {url && <span className="truncate normal-case tracking-[0.08em] text-ivory/40">{url}</span>}
      </figcaption>
      <div className="hatch flex aspect-video w-full items-center justify-center border border-ivory/20 bg-paper/80">
        {shot ? (
          <img src={shot} alt={`huvu's browser at ${url}`} className="size-full object-cover" />
        ) : (
          <span className="font-bodoni italic text-ivory/45">the live view streams here</span>
        )}
      </div>
    </figure>
  );
}

const NUMERALS = ["i.", "ii.", "iii.", "iv.", "v.", "vi.", "vii.", "viii."];

export function Choices({ part, onChoose }: { part: ToolPart; onChoose: (text: string) => void }) {
  const raw = part.args.options;
  const options = Array.isArray(raw) ? raw.filter((o): o is string => typeof o === "string") : [];
  return (
    <div className="flex flex-wrap gap-x-8">
      {options.map((option, i) => (
        <button
          key={option}
          type="button"
          onClick={() => onChoose(option)}
          className="flex min-h-11 items-baseline gap-2 font-bodoni text-[19px] text-ivory transition-colors duration-500 hover:text-sindoor focus-visible:text-sindoor focus-visible:outline-none sm:text-[21px]"
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
