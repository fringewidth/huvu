import { useEffect, useEffectEvent, useRef } from "react";
import { asked, getThread, subscribe } from "../chat/thread";
import { speak } from "./speaker";

// Says every answer aloud, for a screen that always speaks (Fire TV). Each
// turn asked since `since` is said once, from huvu's first written words,
// the way voice mode does. That covers asks from this screen and asks
// that reach it some other way (Alexa). A newer answer cuts off the one
// still being said. Feeds and notices are no answers, and stay quiet.
export function useSpokenAnswers(enabled: boolean, since: number): void {
  const said = useRef(new Set<string>());

  const onThread = useEffectEvent(() => {
    const turn = getThread().turns.findLast(asked);
    if (!turn || turn.at < since || !turn.answer || said.current.has(turn.id)) return;
    said.current.add(turn.id);
    speak(turn.id, false).catch((err) => console.error("voice: playback failed", err));
  });

  useEffect(() => {
    if (!enabled) return;
    return subscribe(() => onThread());
  }, [enabled]);
}
