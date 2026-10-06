import { memo, useLayoutEffect, useRef, useState } from "react";
import * as m from "motion/react-m";
import { AnimatePresence } from "motion/react";
import { errorText } from "../account/session";
import { answerApproval, answerQuestion, useWaiting, type Approval, type Question } from "../account/waiting";
import { capsClass, EASE } from "../stage/style";
import { left, useNow } from "../stage/useNow";

interface WaitingProps {
  // Where the card sits above the bottom edge: on the composer, or above
  // the "write" button in voice mode.
  bottom: number;
  // How tall the card is, so the turn above it can stop short of it.
  onHeight: (px: number) => void;
}

type Item = { kind: "question"; q: Question; until: number } | { kind: "approval"; a: Approval; until: number };

// What waits on this phone's user: an action that needs their yes, or a
// shared device asking whether it's them. It sits pinned above the
// composer until it is answered, here or on another phone, or runs out.
// One shows at a time, the one that runs out first; the count says how
// many more wait. These are the only boxed things on the stage besides the
// lists, since they ask for an answer and must not read as huvu talking.
export const Waiting = memo(function Waiting({ bottom, onHeight }: WaitingProps) {
  const { approvals, questions } = useWaiting();
  const now = useNow(approvals.length + questions.length > 0);
  const ref = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const items: Item[] = [
    ...questions.map((q): Item => ({ kind: "question", q, until: Date.parse(q.expires_at) })),
    ...approvals.map((a): Item => ({ kind: "approval", a, until: Date.parse(a.deadline) })),
  ]
    .filter((item) => item.until > now)
    .sort((x, y) => x.until - y.until);
  const item = items.at(0) ?? null;
  const key = item ? (item.kind === "question" ? item.q.id : item.a.id) : "none";

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => onHeight(el.offsetHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, [onHeight]);

  const answer = async (yes: boolean) => {
    if (!item || busy) return;
    setBusy(true);
    setError(null);
    try {
      if (item.kind === "question") await answerQuestion(item.q.id, yes);
      else await answerApproval(item.a.id, yes);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div ref={ref} style={{ bottom }} className="pointer-events-none absolute inset-x-0 z-20">
      <AnimatePresence mode="wait" initial={false}>
        {item && (
          <m.section
            key={key}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 10 }}
            transition={{ duration: 0.5, ease: EASE }}
            aria-live="assertive"
            className="pointer-events-auto mx-auto mb-3 flex w-[calc(100%-3rem)] max-w-[49rem] flex-col gap-3 border border-ivory/15 bg-paper/95 px-5 pb-2 pt-4 sm:w-[calc(100%-6rem)]"
          >
            <div className="flex items-baseline gap-3">
              <span className={`${capsClass} text-sindoor`}>{item.kind === "question" ? "is it you?" : "approve?"}</span>
              {items.length > 1 && <span className={`${capsClass} text-ivory/45`}>1 of {items.length}</span>}
              <span className={`${capsClass} ml-auto tabular-nums text-ivory/50`}>{left(item.until, now)}</span>
            </div>
            {item.kind === "question" ? <QuestionBody q={item.q} /> : <ApprovalBody a={item.a} />}
            {error && <p className="m-0 font-display text-[13px] font-medium leading-[1.4] text-sindoor">{error}</p>}
            <div className="flex gap-6">
              <button
                type="button"
                disabled={busy}
                onClick={() => void answer(true)}
                className={`${capsClass} -mx-2 flex min-h-11 items-center gap-3 px-2 text-ivory outline-none transition-colors duration-500 hover:text-sindoor focus-visible:text-sindoor disabled:text-ivory/35`}
              >
                <span className="h-px w-5 bg-thread" />
                {item.kind === "question" ? "yes, it's me" : "approve"}
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void answer(false)}
                className={`${capsClass} -mx-2 min-h-11 px-2 text-ivory/60 outline-none transition-colors duration-500 hover:text-ivory focus-visible:text-ivory disabled:text-ivory/30`}
              >
                {item.kind === "question" ? "not me" : "deny"}
              </button>
            </div>
          </m.section>
        )}
      </AnimatePresence>
    </div>
  );
});

function QuestionBody({ q }: { q: Question }) {
  return (
    <>
      <p className="m-0 font-bodoni text-[22px] italic leading-[1.2] text-ivory sm:text-[26px]">Is it you at the {q.device.name}?</p>
      {q.reason && <p className="m-0 font-display text-[14px] font-medium leading-[1.4] text-ivory/75">{q.reason}</p>}
    </>
  );
}

function ApprovalBody({ a }: { a: Approval }) {
  const args = Object.entries(a.args ?? {}).filter(([name]) => name !== "reason");
  return (
    <>
      <p className="m-0 font-bodoni text-[22px] italic leading-[1.2] text-ivory sm:text-[26px]">{a.label}</p>
      {args.length > 0 && (
        <p className="m-0 font-display text-[13px] font-medium leading-[1.4] text-ivory/50">
          {args.map(([name, value]) => `${name.replace(/_/g, " ")}: ${typeof value === "string" ? value : JSON.stringify(value)}`).join(" / ")}
        </p>
      )}
      {a.reason && <p className="m-0 font-display text-[14px] font-medium leading-[1.4] text-ivory/75">{a.reason}</p>}
      <p className="m-0 font-display text-[13px] font-medium leading-[1.4] text-ivory/45">asked by {a.requester}</p>
    </>
  );
}
