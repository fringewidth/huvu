# Drives one tab of a phone's Chrome over the DevTools protocol, forwarded
# to this machine by adb, without attaching to any other tab: Playwright
# attaches to every tab, and a phone's background tabs are frozen, so it
# hangs.
import asyncio, json, base64, itertools, urllib.request, urllib.parse
import websockets

HOST = "http://localhost:9333"

def http(method, path):
    req = urllib.request.Request(HOST + path, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode()
    return json.loads(body) if body.strip().startswith(("{", "[")) else body

class Tab:
    def __init__(self, target):
        self.target = target
        self.ids = itertools.count(1)
        self.pending = {}

    async def _read(self):
        async for msg in self.ws:
            m = json.loads(msg)
            if "id" in m and m["id"] in self.pending:
                self.pending.pop(m["id"]).set_result(m)

    async def send(self, method, **params):
        i = next(self.ids)
        fut = asyncio.get_event_loop().create_future()
        self.pending[i] = fut
        await self.ws.send(json.dumps({"id": i, "method": method, "params": params}))
        m = await asyncio.wait_for(fut, 60)
        if "error" in m:
            raise RuntimeError(f"{method}: {m['error']}")
        return m.get("result", {})

    async def js(self, expr, await_promise=True):
        r = await self.send("Runtime.evaluate", expression=expr, awaitPromise=await_promise, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(json.dumps(r["exceptionDetails"])[:500])
        return r["result"].get("value")

    async def tap(self, x, y):
        await self.send("Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": x, "y": y}])
        await asyncio.sleep(0.05)
        await self.send("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])

    async def key(self, ch):
        if ch == "\n":
            await self.send("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter", windowsVirtualKeyCode=13, text="\r")
            await self.send("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter", windowsVirtualKeyCode=13)
            return
        await self.send("Input.dispatchKeyEvent", type="keyDown", key=ch, text=ch)
        await self.send("Input.dispatchKeyEvent", type="keyUp", key=ch)

    async def insert(self, text):
        await self.send("Input.insertText", text=text)

    async def screenshot(self, path):
        r = await self.send("Page.captureScreenshot", format="png")
        open(path, "wb").write(base64.b64decode(r["data"]))

    @classmethod
    async def attach(cls, url_part):
        t = next(t for t in http("GET", "/json/list") if t["type"] == "page" and url_part in t["url"])
        tab = cls(t)
        tab.ws = await websockets.connect(t["webSocketDebuggerUrl"], max_size=50_000_000)
        tab.reader = asyncio.create_task(tab._read())
        return tab

    async def close(self):
        self.reader.cancel()
        await self.ws.close()
