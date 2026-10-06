import { useState, type FormEvent } from "react";
import * as m from "motion/react-m";
import { defaultDeviceName, errorText, login, useSession } from "../account/session";
import { capsClass, EASE } from "../stage/style";

// A personal device (phone, web) logs in with its user's password. Nobody
// signs up: users are added from the backend's command line. The device
// gets a name its user can tell apart later, in the list of devices.
//
// It sits straight on the garden like the stage does: huvu's line in
// Bodoni, what you type in Syne.
const fieldClass =
  "halo w-full min-w-0 border-0 border-b border-sindoor/40 bg-transparent pb-2 font-display text-[20px] font-medium leading-[1.4] text-ivory caret-sindoor outline-none transition-colors duration-500 placeholder:text-ivory/30 focus:border-sindoor/90 sm:text-[24px]";

export function Login() {
  const { reason } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [deviceName, setDeviceName] = useState(defaultDeviceName);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || !username.trim() || !password) return;
    setBusy(true);
    setError(null);
    try {
      await login(username.trim(), password, deviceName.trim() || defaultDeviceName());
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  };

  return (
    <div className="relative z-10 h-dvh w-full overflow-y-auto text-ivory">
      <span className={`${capsClass} halo absolute left-6 top-7 text-[11px] tracking-[0.42em] text-ivory/75 sm:left-12 sm:top-10`}>huvu</span>
      <m.form
        onSubmit={submit}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.8, ease: EASE }}
        className="mx-auto flex w-full max-w-[52rem] flex-col gap-9 px-6 pb-10 pt-[16dvh] sm:px-12"
      >
        <div className="flex flex-col gap-3">
          <h1 className="halo m-0 font-bodoni text-[34px] font-normal italic leading-[1.1] text-ivory/80 sm:text-[46px]">Who's there?</h1>
          {reason && <p className={`${capsClass} m-0 text-ivory/55`}>{reason}</p>}
        </div>
        <label className="flex max-w-[26rem] flex-col gap-2">
          <span className={`${capsClass} text-sindoor`}>name</span>
          <input
            className={fieldClass}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            autoFocus
          />
        </label>
        <label className="flex max-w-[26rem] flex-col gap-2">
          <span className={`${capsClass} text-sindoor`}>password</span>
          <input className={fieldClass} type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
        </label>
        <label className="flex max-w-[26rem] flex-col gap-2">
          <span className={`${capsClass} text-ivory/55`}>this device is</span>
          <input className={fieldClass} value={deviceName} onChange={(e) => setDeviceName(e.target.value)} maxLength={60} />
        </label>
        <div className="flex flex-col gap-4">
          {error && <p className="m-0 font-display text-[13px] font-medium leading-[1.4] text-sindoor">{error}</p>}
          <button
            type="submit"
            disabled={busy || !username.trim() || !password}
            className={`${capsClass} -mx-2 flex min-h-11 items-center gap-3 self-start px-2 text-ivory outline-none transition-colors duration-500 hover:text-sindoor focus-visible:text-sindoor focus-visible:ring-1 focus-visible:ring-sindoor/60 disabled:text-ivory/35`}
          >
            <span className="h-px w-5 bg-thread" />
            {busy ? "letting you in" : "come in"}
          </button>
        </div>
      </m.form>
    </div>
  );
}
