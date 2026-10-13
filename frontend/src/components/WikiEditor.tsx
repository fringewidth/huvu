import { useMemo, useState } from "react";
import CodeMirror, { EditorView } from "@uiw/react-codemirror";
import { markdown } from "@codemirror/lang-markdown";
import { errorText } from "../account/session";
import { HttpError } from "../net/http";
import { capsClass } from "../stage/style";
import { deletePage, readPage, savePage, type WikiPage } from "../wiki/wiki";
import { buttonClass, type Place } from "./wikiParts";

// The page's Markdown, as huvu wrote it, so an edit changes only what the
// person changed. Saving names the version it was opened at: if huvu
// changed the page meanwhile, the person picks whose text stays.
export function Editing({
  place,
  onDraft,
  onSaved,
  onDeleted,
}: {
  place: Extract<Place, { kind: "edit" }>;
  onDraft: (draft: string) => void;
  onSaved: (page: WikiPage) => void;
  onDeleted: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // huvu's newer version, when a save found one.
  const [theirs, setTheirs] = useState<WikiPage | null>(null);
  const [deleting, setDeleting] = useState(false);
  const extensions = useMemo(() => [markdown(), EditorView.lineWrapping, editorTheme], []);

  const save = async (version: string | null) => {
    setBusy(true);
    setError(null);
    try {
      onSaved(await savePage(place.path, place.draft, version));
    } catch (err) {
      if (err instanceof HttpError && err.status === 409 && place.page) {
        try {
          setTheirs(await readPage(place.path));
        } catch {
          setTheirs(null);
        }
      }
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!place.page) return;
    setBusy(true);
    setError(null);
    try {
      await deletePage(place.path, place.page.version);
      onDeleted();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="border-b border-sindoor/40 pb-2 focus-within:border-sindoor/90">
        <CodeMirror
          value={place.draft}
          onChange={onDraft}
          theme="none"
          extensions={extensions}
          basicSetup={{ lineNumbers: false, foldGutter: false, highlightActiveLine: false, highlightActiveLineGutter: false }}
          autoFocus
        />
      </div>
      {error && <p className="m-0 font-display text-[13px] font-medium leading-[1.4] text-sindoor">{error}</p>}
      {theirs && (
        <div className="flex flex-wrap items-center gap-x-5">
          <span className={`${capsClass} text-sindoor`}>huvu changed this page while you edited</span>
          <button type="button" disabled={busy} onClick={() => void save(theirs.version)} className={`${buttonClass} text-ivory hover:text-sindoor`}>
            keep mine
          </button>
          <button type="button" disabled={busy} onClick={() => onSaved(theirs)} className={`${buttonClass} text-ivory/60 hover:text-ivory`}>
            take huvu's
          </button>
        </div>
      )}
      <div className="flex items-center gap-4">
        <button
          type="button"
          disabled={busy || (place.page !== null && place.draft === place.page.text)}
          onClick={() => void save(place.page?.version ?? null)}
          className={`${buttonClass} flex items-center gap-3 text-ivory hover:text-sindoor disabled:text-ivory/35`}
        >
          <span className="h-px w-5 bg-thread" />
          save
        </button>
        {place.page && !deleting && (
          <button type="button" disabled={busy} onClick={() => setDeleting(true)} className={`${buttonClass} ml-auto text-ivory/45 hover:text-ivory`}>
            delete
          </button>
        )}
        {place.page && deleting && (
          <span className="ml-auto flex items-center gap-4">
            <span className={`${capsClass} text-sindoor`}>delete this page?</span>
            <button type="button" disabled={busy} onClick={() => void remove()} className={`${buttonClass} text-ivory hover:text-sindoor`}>
              yes
            </button>
            <button type="button" onClick={() => setDeleting(false)} className={`${buttonClass} text-ivory/60 hover:text-ivory`}>
              no
            </button>
          </span>
        )}
      </div>
    </>
  );
}

// The editor in huvu's colors: ivory on the page, a sindoor caret.
const editorTheme = EditorView.theme(
  {
    "&": { backgroundColor: "transparent", color: "var(--ivory)", fontSize: "15px" },
    ".cm-content": { fontFamily: "'DM Sans Variable', system-ui, sans-serif", lineHeight: "1.6", caretColor: "var(--sindoor)", padding: "0" },
    ".cm-line": { padding: "0" },
    "&.cm-focused": { outline: "none" },
    ".cm-cursor": { borderLeftColor: "var(--sindoor)" },
    "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection": { backgroundColor: "rgb(224 113 90 / 0.25)" },
  },
  { dark: true },
);

