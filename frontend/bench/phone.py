# The garden bench on a real phone: SECONDS of made-up loud speech grow the
# garden (canvas/bench.ts) while a key is typed every 150ms, and it reports
# how the page felt. Run it on a production build, since a dev build's
# React is several times slower.
#
#   1. Build and serve a bench build (it talks to the backend on this
#      machine, at http://localhost:3001):
#        VITE_BENCH=1 npx vite build --mode web --outDir /tmp/huvu-bench
#        npx vite preview --mode web --outDir /tmp/huvu-bench --port 4173
#   2. Connect the phone with adb (USB, or Wireless debugging: adb pair,
#      then adb connect), and tunnel both ways:
#        adb reverse tcp:4173 tcp:4173
#        adb reverse tcp:3001 tcp:3001
#        adb forward tcp:9333 localabstract:chrome_devtools_remote
#   3. On the phone, open http://localhost:4173/?bench in Chrome and keep it
#      in front with the screen on.
#   4. Run, once per setup:
#        HUVU_USER=... HUVU_PASSWORD=... uv run --with websockets python bench/phone.py <label> ["?bench&..."]
#
# The keydown numbers come from the Event Timing API: from the key to the
# frame that shows it. Events under 16ms aren't reported at all, so the
# slow counts are out of `typed`.
import asyncio, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import Tab

LABEL = sys.argv[1] if len(sys.argv) > 1 else "run"
QUERY = sys.argv[2] if len(sys.argv) > 2 else "?bench"
SECONDS = int(os.environ.get("SECONDS_OF_SPEECH", "20"))
BASE = os.environ.get("BASE", "http://localhost:4173")

SETUP = """(() => {
  window.__m = { events: [], frames: [] };
  new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__m.events.push({ name: e.name, dur: e.duration, at: performance.timeOrigin + e.startTime, wait: e.processingStart - e.startTime, proc: e.processingEnd - e.processingStart }); })
    .observe({ type: 'event', durationThreshold: 16 });
  let last = performance.now();
  const f = (t) => { window.__m.frames.push(t - last); last = t; if (!window.__m.stop) requestAnimationFrame(f); };
  requestAnimationFrame(f);
  return true;
})()"""

def pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * p))], 1) if xs else 0

async def wait_for(tab, expr, secs=30):
    end = time.time() + secs
    while time.time() < end:
        if await tab.js(expr):
            return
        await asyncio.sleep(0.3)
    raise TimeoutError(expr)

async def main():
    tab = await Tab.attach("localhost:")
    try:
        # A fresh page every run: an empty garden and the same random numbers.
        await tab.js(f"location.replace('{BASE}/{QUERY}')")
        await asyncio.sleep(1)
        await wait_for(tab, "document.readyState === 'complete' && !!(document.querySelector('textarea') || document.querySelector('input[type=password]'))")
        if await tab.js("!!document.querySelector('input[type=password]')"):
            await tab.js("document.querySelector('input[autocomplete=username]').focus()")
            await tab.insert(os.environ["HUVU_USER"])
            await tab.js("document.querySelector('input[type=password]').focus()")
            await tab.insert(os.environ["HUVU_PASSWORD"])
            await tab.key("\n")
            await wait_for(tab, "!!document.querySelector('textarea')")
        await asyncio.sleep(2)
        frame_ms = await tab.js("new Promise(r => { const t=[]; const f=(n)=>{t.push(n); if(t.length<90) requestAnimationFrame(f); else r((t[89]-t[0])/89);}; requestAnimationFrame(f); })")
        box = await tab.js("(() => { const r = document.querySelector('textarea').getBoundingClientRect(); return [r.x + r.width/2, r.y + r.height/2]; })()")
        await tab.tap(*box)
        await asyncio.sleep(1.5)
        await tab.js(SETUP)
        t0 = await tab.js("performance.timeOrigin + performance.now()")
        await tab.js(f"window.__bench.speak({SECONDS})")
        typed = 0
        text = "the quick brown fox jumps over the lazy dog "
        end = time.time() + SECONDS
        while time.time() < end:
            await tab.key(text[typed % len(text)])
            typed += 1
            await asyncio.sleep(0.15)
        await tab.js("window.__m.stop = true")
        m = await tab.js("window.__m")
        g = await tab.js("window.__bench.stats")
        await tab.screenshot(f"{LABEL}.png")
    finally:
        await tab.close()
    keys = [e for e in m["events"] if e["name"] == "keydown"]
    frames = m["frames"][1:]
    print(json.dumps({
        "label": LABEL, "typed": typed,
        "keydown_over_16ms": len(keys), "keydown_p50": pct([e["dur"] for e in keys], 0.5), "keydown_max": pct([e["dur"] for e in keys], 1.0),
        "keydown_over_200ms": [(round((e["at"] - t0) / 1000, 2), round(e["dur"])) for e in keys if e["dur"] > 200],
        "page_fps": round(len(frames) / SECONDS, 1), "page_frame_p95": pct(frames, 0.95), "page_frame_max": pct(frames, 1.0),
        "screen_frame_ms": round(frame_ms, 1),
        "petals": g["petals"], "garden_frames": g["frames"],
        "garden_ms_per_frame": round(g["workMs"] / max(1, g["frames"]), 1), "garden_worst_ms": round(g["worstMs"], 1),
        "garden_frames_over_50ms": len(g["long"]),
    }))

asyncio.run(main())
