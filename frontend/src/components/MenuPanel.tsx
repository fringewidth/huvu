import { useEffect, useLayoutEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { allow, confirmPairing, refreshDevices, refreshTriggers, revoke, setPaused, useHousehold, type Trigger } from "../account/household";
import { canPair, errorText, logOut, useReach, useSession } from "../account/session";
import { UNREACHABLE } from "../net/http";
import { release, useWaiting } from "../account/waiting";
import { capsClass } from "../stage/style";
import type { MenuSection } from "./Menu";

const buttonClass = `${capsClass} -mx-2 min-h-11 shrink-0 px-2 outline-none transition-colors duration-500 focus-visible:ring-1 focus-visible:ring-sindoor/60`;

// What the menu shows, loaded the first time it opens: who you are and
// where you're recognized, your triggers, your devices and your groups'
// shared ones, and pairing a shared device from a phone.
export function MenuPanel({ section }: { section: MenuSection }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void refreshTriggers();
    void refreshDevices();
  }, []);

  useLayoutEffect(() => {
    if (section === "triggers") ref.current?.querySelector("#menu-triggers")?.scrollIntoView({ block: "start" });
  }, [section]);

  return (
    <div ref={ref} className="flex flex-col gap-7">
      <You />
      <Triggers />
      <Devices />
    </div>
  );
}

function Section({ id, title, children }: { id?: string; title: string; children: ReactNode }) {
  return (
    <section id={id} className="flex scroll-mt-4 flex-col gap-3">
      <h2 className={`${capsClass} m-0 text-ivory/45`}>{title}</h2>
      {children}
    </section>
  );
}

// Stands in for what huvu hasn't said yet: why, once loading failed. The
// stage already says huvu can't be reached, so here that is short.
function Unknown({ trouble }: { trouble: string | null }) {
  const text = trouble === UNREACHABLE ? "Unknown until huvu answers." : (trouble ?? "A moment.");
  return <p className="m-0 font-bodoni text-[19px] italic text-ivory/45">{text}</p>;
}

function You() {
  const { me } = useSession();
  const reach = useReach();
  const { places } = useWaiting();
  const [error, setError] = useState<string | null>(null);
  const leave = async (deviceId: string) => {
    setError(null);
    try {
      await release(deviceId);
    } catch (err) {
      setError(errorText(err));
    }
  };
  return (
    <Section title="you">
      <div className="flex items-baseline gap-3">
        {me ? (
          <span className="font-bodoni text-[24px] italic leading-[1.1] text-ivory">{me.user?.name ?? "you"}</span>
        ) : (
          <Unknown trouble={reach === "down" ? UNREACHABLE : null} />
        )}
        {me && <span className={`${capsClass} text-ivory/45`}>on {me.device.name}</span>}
        <button type="button" onClick={() => void logOut()} className={`${buttonClass} ml-auto text-ivory/60 hover:text-ivory`}>
          log out
        </button>
      </div>
      {/* Where you are recognized, so you can end it from here when you
          walk away from the TV. */}
      {places.map((p) => (
        <div key={p.device.id} className="flex items-baseline gap-3">
          <span className={`${capsClass} text-sindoor`}>recognized</span>
          <span className="font-display text-[15px] font-medium text-ivory/85">at the {p.device.name}</span>
          <button type="button" onClick={() => void leave(p.device.id)} className={`${buttonClass} ml-auto text-ivory/60 hover:text-ivory`}>
            not there
          </button>
        </div>
      ))}
      {error && <p className="m-0 font-display text-[13px] font-medium leading-[1.4] text-sindoor">{error}</p>}
    </Section>
  );
}

function Triggers() {
  const { triggers, trouble } = useHousehold();
  return (
    <Section id="menu-triggers" title={triggers ? `triggers / ${triggers.length}` : "triggers"}>
      {!triggers && <Unknown trouble={trouble} />}
      {triggers?.length === 0 && <p className="m-0 font-bodoni text-[19px] italic text-ivory/45">None yet. Ask huvu to keep an eye on something.</p>}
      {triggers?.map((t) => (
        <TriggerRow key={t.id} trigger={t} />
      ))}
    </Section>
  );
}

function TriggerRow({ trigger: t }: { trigger: Trigger }) {
  const [error, setError] = useState<string | null>(null);
  const limited = t.status === "paused" && t.paused_by === "limit";
  const toggle = async () => {
    setError(null);
    try {
      if (t.status === "awaiting") await allow(t.id);
      else await setPaused(t.id, t.status === "active");
    } catch (err) {
      setError(errorText(err));
    }
  };
  const state = describe(t);
  return (
    <div className={`flex flex-col gap-1.5 border-l pl-3 ${limited ? "border-sindoor" : "border-ivory/15"}`}>
      <p className="m-0 line-clamp-3 font-display text-[15px] font-medium leading-[1.4] text-ivory/85">{t.note}</p>
      <div className="flex items-center gap-3">
        <span className={`${capsClass} ${limited || t.status === "awaiting" ? "text-sindoor" : "text-ivory/45"}`}>{state}</span>
        {t.fire_count > 0 && <span className={`${capsClass} text-ivory/35`}>fired {t.fire_count}</span>}
        {t.status !== "unconfirmed" && (
          <button
            type="button"
            onClick={() => void toggle()}
            className={`${buttonClass} ml-auto ${limited ? "text-ivory hover:text-sindoor" : "text-ivory/60 hover:text-ivory"}`}
          >
            {t.status === "awaiting" ? "allow" : t.status === "active" ? "pause" : "resume"}
          </button>
        )}
      </div>
      {error && <p className="m-0 font-display text-[13px] font-medium leading-[1.4] text-sindoor">{error}</p>}
    </div>
  );
}

function describe(t: Trigger): string {
  let state = `watching / ${t.once ? "once" : "every time"}`;
  if (t.tier !== "basic") state += ` / ${t.tier}`;
  if (t.status === "unconfirmed") state = "waiting for its person to say it's them";
  else if (t.status === "awaiting") state = "waiting for your yes: it uses the advanced model";
  else if (t.paused_by === "limit") state = "paused: it ran too often";
  else if (t.status === "paused") state = `paused by ${t.paused_by === "user" ? "you" : "huvu"}`;
  return state;
}

function Devices() {
  const { me } = useSession();
  const { devices, trouble } = useHousehold();
  const [error, setError] = useState<string | null>(null);
  const drop = async (id: string) => {
    setError(null);
    try {
      await revoke(id);
    } catch (err) {
      setError(errorText(err));
    }
  };
  return (
    <Section title="devices">
      {!devices && <Unknown trouble={trouble} />}
      {devices?.map((d) => {
        const mine = d.id === me?.device.id;
        return (
          <div key={d.id} className="flex items-baseline gap-3">
            <span className="min-w-0 truncate font-bodoni text-[19px] text-ivory">{d.name}</span>
            <span className={`${capsClass} shrink-0 text-ivory/40`}>{mine ? "this one" : d.group ? `${d.kind} / ${d.group}` : d.kind}</span>
            {!mine && (
              <button type="button" onClick={() => void drop(d.id)} className={`${buttonClass} ml-auto text-ivory/60 hover:text-ivory`}>
                log out
              </button>
            )}
          </div>
        );
      })}
      {error && <p className="m-0 font-display text-[13px] font-medium leading-[1.4] text-sindoor">{error}</p>}
      {canPair(me) && <PairDevice />}
    </Section>
  );
}

// A phone pairs a shared device: type the code the TV shows. A member of
// more than one group says which group the device joins.
function PairDevice() {
  const { me } = useSession();
  const groups = Object.entries(me?.groups ?? {})
    .filter(([, role]) => role === "member")
    .map(([id]) => id);
  const [code, setCode] = useState("");
  const [group, setGroup] = useState<string | null>(null);
  const [said, setSaid] = useState<{ text: string; ok: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const needsGroup = groups.length > 1;
  const ready = code.replace(/[^a-z0-9]/gi, "").length === 8 && (!needsGroup || group !== null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!ready || busy) return;
    setBusy(true);
    setSaid(null);
    try {
      const device = await confirmPairing(code, needsGroup ? group : null);
      setSaid({ text: `paired the ${device.name}`, ok: true });
      setCode("");
    } catch (err) {
      setSaid({ text: errorText(err), ok: false });
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="mt-2 flex flex-col gap-3 border-t border-ivory/10 pt-4">
      <label className="flex flex-col gap-2">
        <span className={`${capsClass} text-sindoor`}>pair a device / the code it shows</span>
        <input
          value={code}
          onChange={(e) => setCode(e.target.value.toUpperCase())}
          placeholder="ABCD-EF23"
          autoCapitalize="characters"
          autoCorrect="off"
          spellCheck={false}
          maxLength={12}
          className="w-full border-0 border-b border-sindoor/40 bg-transparent pb-1.5 font-display text-[20px] font-medium tracking-[0.12em] text-ivory caret-sindoor outline-none transition-colors duration-500 placeholder:text-ivory/25 focus:border-sindoor/90"
        />
      </label>
      {needsGroup && (
        <div className="flex flex-wrap items-baseline gap-x-5">
          <span className={`${capsClass} text-ivory/45`}>joins</span>
          {groups.map((g) => (
            <button
              key={g}
              type="button"
              onClick={() => setGroup(g)}
              className={`${buttonClass} ${group === g ? "text-sindoor" : "text-ivory/60 hover:text-ivory"}`}
            >
              {g}
            </button>
          ))}
        </div>
      )}
      <div className="flex items-center gap-4">
        <button
          type="submit"
          disabled={!ready || busy}
          className={`${buttonClass} flex items-center gap-3 text-ivory hover:text-sindoor disabled:text-ivory/35`}
        >
          <span className="h-px w-5 bg-thread" />
          pair
        </button>
        {said && <span className={`font-display text-[13px] font-medium leading-[1.4] ${said.ok ? "text-ivory/70" : "text-sindoor"}`}>{said.text}</span>}
      </div>
    </form>
  );
}
