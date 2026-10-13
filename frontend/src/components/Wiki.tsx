import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import wikiLink from "@flowershow/remark-wiki-link";
import { errorText } from "../account/session";
import { capsClass } from "../stage/style";
import { listWiki, readPage, setWikiBack, setWikiOpen, type WikiListing, type WikiPage } from "../wiki/wiki";
import { buttonClass, type Place } from "./wikiParts";

// The editor, CodeMirror, is most of the wiki's weight, and most visits
// only read: it loads the first time a page is edited.
const Editing = lazy(() => import("./WikiEditor").then((mod) => ({ default: mod.Editing })));

// huvu's wiki of its person, over the stage: the contents (the agent's own
// index from MEMORY.md, then every page, newest first), a page set as
// huvu's writing, and an editor for the page's Markdown. The pages are the
// agent's; the person reads them and corrects what is wrong.
export function WikiView() {
  const [listing, setListing] = useState<WikiListing | null>(null);
  const [place, setPlace] = useState<Place>({ kind: "contents" });
  const [trouble, setTrouble] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);

  const refresh = useCallback(() => listWiki().then(setListing, (err: unknown) => setTrouble(errorText(err))), []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // A new view starts at its top.
  const shown = place.kind === "contents" ? "" : place.kind === "edit" ? `edit:${place.path}` : place.page.path;
  useEffect(() => {
    scroller.current?.scrollTo({ top: 0 });
  }, [shown]);

  const open = useCallback(async (path: string) => {
    setTrouble(null);
    try {
      setPlace({ kind: "page", page: await readPage(path) });
    } catch (err) {
      setTrouble(errorText(err));
    }
  }, []);

  // Back walks out one step: an edit to its page, a page to the contents,
  // the contents out of the wiki.
  useEffect(() => {
    setWikiBack(() => {
      if (place.kind === "edit") setPlace(place.page ? { kind: "page", page: place.page } : { kind: "contents" });
      else if (place.kind === "page") setPlace({ kind: "contents" });
      else return false;
      return true;
    });
    return () => setWikiBack(null);
  }, [place]);

  const files = useMemo(() => listing?.pages.map((p) => p.path) ?? [], [listing]);

  return (
    <div className="absolute inset-0 z-40 flex flex-col bg-paper">
      <header className="flex items-center gap-4 border-b border-ivory/10 px-4 pb-2 pt-3.5 sm:px-10 sm:pt-[26px]">
        <button type="button" onClick={() => setPlace({ kind: "contents" })} className={`${buttonClass} text-[11px] tracking-[0.42em] text-ivory/75 hover:text-ivory`}>
          wiki
        </button>
        {place.kind !== "contents" && <span className={`${capsClass} min-w-0 truncate text-ivory/40`}>{place.kind === "edit" ? place.path : place.page.path}</span>}
        <button type="button" onClick={() => setWikiOpen(false)} className={`${buttonClass} ml-auto text-ivory/60 hover:text-ivory`}>
          close
        </button>
      </header>
      <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto px-4 pb-16 pt-6 [scrollbar-width:thin] sm:px-10">
        <div className="mx-auto flex max-w-[42rem] flex-col gap-7">
          {trouble && <p className="m-0 font-display text-[13px] font-medium leading-[1.4] text-sindoor">{trouble}</p>}
          {place.kind === "contents" && (
            <Contents
              listing={listing}
              onOpen={(path) => void open(path)}
              onNew={(path) => setPlace({ kind: "edit", page: null, path, draft: `# ${titleOf(path)}\n\n` })}
            />
          )}
          {place.kind === "page" && (
            <Reading page={place.page} files={files} onOpen={(path) => void open(path)} onEdit={() => setPlace({ kind: "edit", page: place.page, path: place.page.path, draft: place.page.text })} />
          )}
          {place.kind === "edit" && (
            <Suspense fallback={<span className={`${capsClass} text-ivory/45`}>a moment</span>}>
              <Editing
                key={place.path}
                place={place}
                onDraft={(draft) => setPlace({ ...place, draft })}
                onSaved={(page) => {
                  setPlace({ kind: "page", page });
                  void refresh();
                }}
                onDeleted={() => {
                  setPlace({ kind: "contents" });
                  void refresh();
                }}
              />
            </Suspense>
          )}
        </div>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3">
      <h2 className={`${capsClass} m-0 text-ivory/45`}>{title}</h2>
      {children}
    </section>
  );
}

// The index first, as huvu wrote it: an entry that starts with a page's
// path opens it. Then every page, newest first.
function Contents({ listing, onOpen, onNew }: { listing: WikiListing | null; onOpen: (path: string) => void; onNew: (path: string) => void }) {
  const [naming, setNaming] = useState<string | null>(null);
  if (!listing) return <p className="m-0 font-bodoni text-[19px] italic text-ivory/45">A moment.</p>;
  const paths = new Set(listing.pages.map((p) => p.path));
  const name = naming?.trim().replace(/^\/+/, "") ?? "";
  const newPath = name && !name.endsWith(".md") ? `${name}.md` : name;
  return (
    <>
      <Section title="index">
        {listing.index.length === 0 && <p className="m-0 font-bodoni text-[19px] italic text-ivory/45">huvu hasn't written an index yet. Its first dream will.</p>}
        {listing.index.map((entry, i) => {
          const path = entry.match(/^\s*(?:[-*]\s*)?`?([\w./ -]+\.md)`?/)?.[1];
          const linked = path && paths.has(path);
          return (
            <p key={i} className="m-0 font-reading text-[16px] leading-[1.5] text-ivory/80">
              {linked ? (
                <button type="button" onClick={() => onOpen(path)} className="text-left underline decoration-sindoor/40 underline-offset-4 outline-none hover:decoration-sindoor focus-visible:ring-1 focus-visible:ring-sindoor/60">
                  {entry}
                </button>
              ) : (
                entry
              )}
            </p>
          );
        })}
      </Section>
      <Section title={`pages / ${listing.pages.length}`}>
        {listing.pages.map((p) => (
          <button key={p.path} type="button" onClick={() => onOpen(p.path)} className="group -mx-2 flex items-baseline gap-3 px-2 py-1 text-left outline-none focus-visible:ring-1 focus-visible:ring-sindoor/60">
            <span className="min-w-0 truncate font-bodoni text-[19px] italic text-ivory transition-colors duration-500 group-hover:text-sindoor">{p.title}</span>
            <span className={`${capsClass} ml-auto shrink-0 text-ivory/35`}>{when(p.modified)}</span>
          </button>
        ))}
        {naming === null ? (
          <button type="button" onClick={() => setNaming("")} className={`${buttonClass} flex items-center gap-3 self-start text-ivory/60 hover:text-ivory`}>
            <span className="h-px w-5 bg-thread" />
            new page
          </button>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (newPath && !paths.has(newPath)) onNew(newPath);
            }}
            className="flex items-baseline gap-4"
          >
            <input
              autoFocus
              value={naming}
              onChange={(e) => setNaming(e.target.value)}
              placeholder="people/mom"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              className="min-w-0 flex-1 border-0 border-b border-sindoor/40 bg-transparent pb-1.5 font-display text-[17px] font-medium text-ivory caret-sindoor outline-none placeholder:text-ivory/25 focus:border-sindoor/90"
            />
            <button type="submit" disabled={!newPath || paths.has(newPath)} className={`${buttonClass} text-ivory hover:text-sindoor disabled:text-ivory/35`}>
              {paths.has(newPath) ? "exists" : "write"}
            </button>
          </form>
        )}
      </Section>
    </>
  );
}

function Reading({ page, files, onOpen, onEdit }: { page: WikiPage; files: string[]; onOpen: (path: string) => void; onEdit: () => void }) {
  // Links between pages open in place: [[wikilinks]] resolved against the
  // wiki's pages as Obsidian does, and plain links to a page's path,
  // relative to the page they are on. Anything else opens outside huvu.
  const components = useMemo<Components>(
    () => ({
      a({ node, href, children, className }) {
        // The wikilink plugin puts the page it resolved to on the link.
        const target = node?.properties?.["data-fs-resolved-file-path"] ?? node?.properties?.dataFsResolvedFilePath;
        const internal = typeof target === "string" ? target : href && isPage(href) ? resolve(page.path, href) : null;
        if (internal || className?.includes("internal")) {
          const missing = !internal || !files.includes(internal);
          return (
            <button
              type="button"
              disabled={missing}
              onClick={() => internal && onOpen(internal)}
              className={`inline text-left underline underline-offset-4 outline-none focus-visible:ring-1 focus-visible:ring-sindoor/60 ${missing ? "decoration-ivory/20 text-ivory/50" : "decoration-sindoor/50 hover:decoration-sindoor"}`}
            >
              {children}
            </button>
          );
        }
        return (
          <a href={href} target="_blank" rel="noreferrer" className="underline decoration-ivory/30 underline-offset-4 hover:decoration-ivory">
            {children}
          </a>
        );
      },
      h1: ({ children }) => <h1 className="m-0 mb-1 font-bodoni text-[30px] font-normal italic leading-[1.15] text-ivory">{children}</h1>,
      h2: ({ children }) => <h2 className={`${capsClass} m-0 mt-4 text-sindoor`}>{children}</h2>,
      h3: ({ children }) => <h3 className="m-0 mt-2 font-display text-[16px] font-semibold text-ivory/90">{children}</h3>,
      p: ({ children }) => <p className="m-0">{children}</p>,
      ul: ({ children }) => <ul className="m-0 flex list-disc flex-col gap-1.5 pl-5 marker:text-sindoor/60">{children}</ul>,
      ol: ({ children }) => <ol className="m-0 flex list-decimal flex-col gap-1.5 pl-5 marker:text-ivory/40">{children}</ol>,
      blockquote: ({ children }) => <blockquote className="m-0 border-l border-sindoor/50 pl-4 text-ivory/70">{children}</blockquote>,
      code: ({ children, className }) =>
        className ? <code className="block whitespace-pre font-mono text-[13px]">{children}</code> : <code className="font-mono text-[0.85em] text-marigold/90">{children}</code>,
      pre: ({ children }) => <pre className="m-0 overflow-x-auto border border-ivory/10 px-3 py-2 text-ivory/80">{children}</pre>,
      table: ({ children }) => (
        <div className="overflow-x-auto">
          <table className="border-collapse font-display text-[14px]">{children}</table>
        </div>
      ),
      th: ({ children }) => <th className={`${capsClass} border-b border-ivory/15 px-3 py-1.5 text-left text-ivory/50`}>{children}</th>,
      td: ({ children }) => <td className="border-b border-ivory/10 px-3 py-1.5 align-top">{children}</td>,
      hr: () => <hr className="my-2 w-12 border-0 border-t border-thread" />,
    }),
    [page.path, files, onOpen],
  );
  return (
    <>
      <article className="flex flex-col gap-4 font-reading text-[18px] leading-[1.55] text-ivory/90 [text-wrap:pretty] sm:text-[19px]">
        <Markdown
          remarkPlugins={[remarkGfm, [wikiLink, { format: "shortestPossible", files, urlResolver: ({ filePath }: { filePath: string }) => filePath }]]}
          components={components}
        >
          {page.text}
        </Markdown>
      </article>
      <div className="flex items-center gap-4 border-t border-ivory/10 pt-3">
        <span className={`${capsClass} text-ivory/35`}>{when(page.modified)}</span>
        <button type="button" onClick={onEdit} className={`${buttonClass} ml-auto text-ivory/60 hover:text-ivory`}>
          edit
        </button>
      </div>
    </>
  );
}

function isPage(href: string): boolean {
  return !/^[a-z][a-z0-9+.-]*:/i.test(href) && !href.startsWith("#") && /\.md(#.*)?$/.test(href);
}

// A link's page, relative to the page it is on.
function resolve(from: string, href: string): string {
  const target = decodeURIComponent(href.replace(/#.*$/, ""));
  const parts = target.startsWith("/") ? [] : from.split("/").slice(0, -1);
  for (const part of target.split("/")) {
    if (part === "..") parts.pop();
    else if (part && part !== ".") parts.push(part);
  }
  return parts.join("/");
}

function titleOf(path: string): string {
  const name = path.split("/").pop()?.replace(/\.md$/, "") ?? path;
  return name.replace(/[-_]+/g, " ");
}

function when(iso: string): string {
  const then = new Date(iso);
  const days = Math.floor((Date.now() - then.getTime()) / 86_400_000);
  if (days < 1) return then.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (days < 7) return then.toLocaleDateString([], { weekday: "short" });
  return then.toLocaleDateString([], { day: "numeric", month: "short" });
}
