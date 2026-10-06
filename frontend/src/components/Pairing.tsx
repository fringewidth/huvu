import { useEffect, useState } from "react";
import * as m from "motion/react-m";
import { AnimatePresence } from "motion/react";
import { checkPairing, errorText, startPairing, useSession, type Pairing as Code } from "../account/session";
import { HttpError } from "../net/http";
import { capsClass, EASE } from "../stage/style";

// A shared device (the TV) can't type a password, so it pairs. It shows a
// code, large, where the greeting would be, and a member types it into
// their phone. The device asks every 2 s whether that happened. A code
// lasts 10 minutes; once it expires (410) the device starts again with a
// new one.
const POLL_MS = 2000;
// While the backend isn't answering.
const RETRY_MS = 5000;

export function Pairing() {
  const { reason } = useSession();
  const [code, setCode] = useState<Code | null>(null);
  const [trouble, setTrouble] = useState<string | null>(null);

  useEffect(() => {
    let dead = false;
    let timer = 0;
    let current: Code | null = null;
    const later = (ms: number) => {
      if (!dead) timer = window.setTimeout(tick, ms);
    };
    // One step: get a code if there is none, else look at the one shown.
    async function tick() {
      try {
        if (!current) {
          current = await startPairing();
          if (!dead) setCode(current);
        } else if (await checkPairing(current.pairing)) {
          return;
        }
        if (!dead) setTrouble(null);
        later(POLL_MS);
      } catch (err) {
        if (err instanceof HttpError && err.status === 410) {
          current = null;
          later(0);
        } else {
          if (!dead) setTrouble(errorText(err));
          later(RETRY_MS);
        }
      }
    }
    void tick();
    return () => {
      dead = true;
      window.clearTimeout(timer);
    };
  }, []);

  return (
    <div className="relative z-10 h-dvh w-full overflow-hidden text-ivory">
      <span className={`${capsClass} halo absolute left-6 top-7 text-[11px] tracking-[0.42em] text-ivory/75 sm:left-12 sm:top-10`}>huvu</span>
      <div className="mx-auto mt-[28dvh] flex w-full max-w-[52rem] flex-col gap-[22px] px-6 sm:px-12">
        <span className="halo font-bodoni text-[60px] italic leading-[1.02] tracking-[-0.02em] text-ivory">Pair this screen.</span>
        <AnimatePresence mode="wait" initial={false}>
          <m.span
            key={code?.code ?? "none"}
            initial={{ opacity: 0 }}
            animate={{ opacity: code ? 1 : 0.3 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.6, ease: EASE }}
            className="halo font-display text-[88px] font-medium leading-none tracking-[0.12em] text-ivory"
          >
            {code?.code ?? "····-····"}
          </m.span>
        </AnimatePresence>
        <span className="flex items-baseline gap-3">
          <span className={`${capsClass} text-sindoor`}>on your phone</span>
          <span className="halo font-display text-[15px] font-medium text-ivory/85">open huvu's menu, pick pair a device, and type this code</span>
        </span>
        {(trouble || reason) && <span className="font-display text-[13px] font-medium leading-[1.4] text-ivory/55">{trouble ?? reason}</span>}
      </div>
    </div>
  );
}
