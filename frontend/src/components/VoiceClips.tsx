import { useEffect, useRef } from "react";
import { capsClass } from "../stage/style";
import { amount, clips, promptOf, setClipsOpen, useClips } from "../voice/clips";
import { PHRASE, STEPS, TALK_TEXT } from "../voice/clipSteps";
import { buttonClass } from "./wikiParts";

// Recording your voice for "hey huvu", over the stage: what it is for and
// how far you got, then one step at a time, with huvu saying what to do
// (voice/clips.ts). Anyone in the family can do it from their own phone or
// browser, and each person's clips make the wake word hear them better.
export function VoiceClipsView() {
  const state = useClips();

  useEffect(() => {
    void clips.load();
    // A phone put away mid-step stops recording.
    const hidden = () => {
      if (document.visibilityState === "hidden") clips.pause();
    };
    document.addEventListener("visibilitychange", hidden);
    return () => {
      document.removeEventListener("visibilitychange", hidden);
      clips.close();
    };
  }, []);

  const step = STEPS[state.step];
  return (
    <div className="absolute inset-0 z-40 flex flex-col bg-paper">
      <header className="flex items-center gap-4 border-b border-ivory/10 px-4 pb-2 pt-3.5 sm:px-10 sm:pt-[26px]">
        <span className={`${capsClass} text-[11px] tracking-[0.42em] text-ivory/75`}>your voice</span>
        <button type="button" onClick={() => setClipsOpen(false)} className={`${buttonClass} ml-auto text-ivory/60 hover:text-ivory`}>
          close
        </button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-16 pt-6 [scrollbar-width:thin] sm:px-10">
        <div className="mx-auto flex max-w-[42rem] flex-col gap-7">
          {state.trouble && <p className="m-0 font-display text-[13px] font-medium leading-[1.4] text-sindoor">{state.trouble}</p>}
          {state.phase === "loading" && <p className="m-0 font-bodoni text-[19px] italic text-ivory/45">A moment.</p>}
          {state.phase === "running" ? <Recording /> : state.phase !== "loading" && <Overview />}
          {state.phase === "running" && step.talk && <p className="m-0 font-reading text-[19px] leading-[1.55] text-ivory/85">{TALK_TEXT}</p>}
        </div>
      </div>
    </div>
  );
}

function Overview() {
  const state = useClips();
  const begun = STEPS.some((s) => (state.counts[s.id] ?? 0) > 0);
  return (
    <>
      {state.phase === "done" ? (
        <p className="m-0 font-bodoni text-[24px] italic leading-[1.25] text-ivory">That's everything. Thank you.</p>
      ) : (
        <p className="m-0 font-bodoni text-[24px] italic leading-[1.25] text-ivory">Teach huvu how you say “{PHRASE}”.</p>
      )}
      <div className="flex flex-col gap-3 font-display text-[15px] leading-[1.55] text-ivory/80">
        <p className="m-0">
          huvu learns to hear its name from people saying it. This takes about ten minutes. You say “{PHRASE}” close to the phone, from across the room, from the next room and
          over noise. Then you read a few lines that sound close to it, and talk for two minutes.
        </p>
        <p className="m-0">
          huvu says each step out loud, and chimes for each one it keeps, so you can follow it away from the screen. Start in a quiet room. You can stop at any time and go
          on later from where you were.
        </p>
        <p className="m-0 text-ivory/55">The clips stay on huvu's own computer, and are used only to teach it the wake word.</p>
        {state.phase === "done" && <p className="m-0 text-ivory/55">Anyone else in the family can do this too, from their own phone or browser.</p>}
      </div>
      {state.phase !== "done" && (
        <button
          type="button"
          onClick={() => void clips.start(state.step)}
          className={`${buttonClass} self-start border border-sindoor/60 px-4 text-[11px] text-ivory hover:text-sindoor`}
        >
          {begun ? "go on" : "begin"}
        </button>
      )}
      <ol className="m-0 flex list-none flex-col gap-1 p-0">
        {STEPS.map((s, i) => {
          const have = amount(s, state.counts);
          return (
            <li key={s.id} className="flex items-baseline gap-3">
              <span className={`font-display text-[15px] font-medium ${have >= s.target ? "text-ivory/45" : "text-ivory/85"}`}>{s.title}</span>
              <span className={`${capsClass} text-ivory/40`}>
                {have} / {s.target}
                {s.talk ? " s" : ""}
              </span>
              <button type="button" onClick={() => void clips.start(i)} className={`${buttonClass} ml-auto text-ivory/60 hover:text-ivory`}>
                {have >= s.target ? "more" : "start here"}
              </button>
            </li>
          );
        })}
      </ol>
    </>
  );
}

function Recording() {
  const state = useClips();
  const step = STEPS[state.step];
  const prompt = promptOf(step, state.counts);
  const have = amount(step, state.counts);
  return (
    <>
      <div className="flex flex-col gap-2">
        <span className={`${capsClass} text-ivory/45`}>
          step {state.step + 1} of {STEPS.length}
        </span>
        <h2 className="m-0 font-bodoni text-[30px] font-normal italic leading-[1.1] text-ivory">{step.title}</h2>
      </div>
      <p className="m-0 font-display text-[16px] leading-[1.55] text-ivory/85">{step.how}</p>
      {!step.talk && (
        <div className="flex flex-col gap-1">
          <span className="font-display text-[34px] font-medium leading-[1.15] text-ivory">{step.kind === "wake" ? PHRASE : prompt}</span>
          {step.kind === "wake" && prompt && <span className="font-bodoni text-[22px] italic text-sindoor">{prompt}</span>}
        </div>
      )}
      <div className="flex flex-col gap-2">
        <div className="flex items-baseline gap-3">
          <span className="font-display text-[22px] font-medium text-ivory">
            {have} of {step.target}
            {step.talk ? " seconds" : ""}
          </span>
          <span className={`${capsClass} ${state.listening ? "text-sindoor" : "text-ivory/45"}`}>{state.listening ? "listening" : "quiet for a moment"}</span>
        </div>
        <Meter />
        {state.note && <p className={`m-0 font-display text-[14px] font-medium leading-[1.4] ${state.note.kept ? "text-ivory/60" : "text-sindoor"}`}>{state.note.text}</p>}
      </div>
      <div className="flex flex-wrap gap-x-6">
        <button type="button" onClick={() => clips.pause()} className={`${buttonClass} text-ivory/70 hover:text-ivory`}>
          pause
        </button>
        {state.canUndo && (
          <button type="button" onClick={() => clips.undo()} className={`${buttonClass} text-ivory/70 hover:text-ivory`}>
            that wasn't it
          </button>
        )}
        <button type="button" onClick={() => clips.skip()} className={`${buttonClass} text-ivory/70 hover:text-ivory`}>
          skip this step
        </button>
      </div>
    </>
  );
}

// How loud the mic hears you, against the room's own noise (the line).
// Drawn straight to the DOM, since it changes every 20 ms.
function Meter() {
  const bar = useRef<HTMLDivElement>(null);
  const line = useRef<HTMLDivElement>(null);
  useEffect(
    () =>
      clips.onLevel((rms, floor) => {
        // A log scale, so a whisper across the room still moves it.
        const scale = (v: number) => Math.max(0, Math.min(1, (Math.log10(Math.max(v, 1e-4)) + 4) / 4));
        if (bar.current) bar.current.style.transform = `scaleX(${scale(rms)})`;
        if (line.current) line.current.style.left = `${scale(floor) * 100}%`;
      }),
    [],
  );
  return (
    <div className="relative h-1.5 w-full overflow-hidden bg-ivory/10">
      <div ref={bar} className="absolute inset-0 origin-left scale-x-0 bg-sindoor/80 transition-transform duration-75" />
      <div ref={line} className="absolute inset-y-0 w-px bg-ivory/60" />
    </div>
  );
}
