"""The external computer: a screen outside Hermes that the model sees and
drives, through the `external_computer` tool the huvu Hermes plugin adds.

Hermes has a computer of its own (its Bot Desktop, where its browser runs),
so this one is named external. The only one is the home theatre's Fire TV
Stick, worked over ADB (`AdbComputer`). The plugin posts each action to the
backend (`POST /api/computer`), the backend runs it here, and the plugin
hands the model the result, with a screenshot as an image when there is one,
so the model sees the screen in the same step. A session may use it only
where it may use the browser: on pro or advanced (`Kernel.may_browse`).

The model sees the screen as elements: the accessibility tree of the window
in front, one line per thing on it, numbered when it can be selected
(`elements`). It is the TV's DOM: on Amazon's own screens (the home screen,
search, settings, the Appstore) it names everything, and a few hundred
tokens of it replace a screenshot. Video apps (YouTube, Netflix, JioHotstar)
draw their own screens and leave it empty, so there a screenshot comes too.
Above the primitives sit the box's common tasks, each one call: `search`,
`settings`, `install` and `media`, built from the steps a person takes with
the remote on Fire OS 5.2.9.5, as read off the family's Stick on 2026-11-01.
Fire OS locks its own search and settings intents to Amazon's apps.

The Stick takes one ADB client at a time, so huvu holds the connection for
everything done to it, and nothing else may: Home Assistant's Android TV
integration was removed for it (2026-10-17). The Stick sits on the AV power
and is off most of the day. `watch` tries it every WATCH_EVERY while it is
off, and checks the connection while it is on. On the first connection
after the Stick booted, it opens huvu's TV app (`open_on_boot`), the way
Home Assistant's `home_theatre_huvu` used to: ADB answers about 26 s after
power, before the home screen, which would cover an app opened then, so it
waits for an app in front first.

Every action is one shell command and one round trip: huvu's helper
(`tv-helper/` beside this package, built into `tv-helper.jar`), run with
`app_process`, presses the keys or selects the element, waits for the screen
to settle, and answers the tree and a screenshot, all in one Java VM. The
Stick's adbd is Android 5.1's: it sends 4 KB a packet and waits for an
acknowledgment after each, so it moves about 160 KB/s over the home Wi-Fi
whatever the link's speed. A `screencap` PNG is 1.3 MB, 9 to 11 s (measured
2026-10-17), so the helper scales the screen on the box and sends a JPEG of
about 80 KB. It replaced a second of `screenrecord` per screenshot (about 3
s), whose recordings hung for runs of calls (seen 2026-10-18 to 2026-11-01)
and cost 18 s a screenshot each time. An action takes about 1.5 to 2.5 s.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import hashlib
import json
import logging
import re
import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("huvu.external_computer")

# The size of the screenshots the model sees, and of the space its clicks
# are in, whatever the screen's own size.
SHOT_WIDTH = 1280
MAX_WAIT = 30.0
# Screenshots kept on disk, so the model can show one to its person.
KEEP_SHOTS = 100

# Key names as the model writes them, lowercased with spaces, hyphens and
# underscores dropped, to Android's key codes.
KEYS = {
    "up": 19,
    "arrowup": 19,
    "down": 20,
    "arrowdown": 20,
    "left": 21,
    "arrowleft": 21,
    "right": 22,
    "arrowright": 22,
    "return": 23,
    "enter": 23,
    "select": 23,
    "ok": 23,
    "escape": 4,
    "esc": 4,
    "back": 4,
    "home": 3,
    "menu": 82,
    "backspace": 67,
    "delete": 67,
    "space": 62,
    "tab": 61,
    "search": 84,
    "pageup": 92,
    "pagedown": 93,
    "playpause": 85,
    "play": 126,
    "pause": 127,
    "stop": 86,
    "next": 87,
    "previous": 88,
    "prev": 88,
    "rewind": 89,
    "fastforward": 90,
    "forward": 90,
    "wake": 224,
    "wakeup": 224,
    "sleep": 223,
}

# What `media` does, as the remote's media keys. Fire OS lets no shell
# process drive a media session itself ("Missing permission to control
# media"), so the keys go to the app in front, which hands them to its own.
MEDIA = {
    "state": None,
    "play": 126,
    "pause": 127,
    "playpause": 85,
    "stop": 86,
    "forward": 90,
    "back": 89,
    "next": 87,
    "previous": 88,
}
# Android's playback states, as `dumpsys media_session` numbers them.
PLAYBACK = {0: "none", 1: "stopped", 2: "paused", 3: "playing", 4: "fast forwarding", 5: "rewinding", 6: "buffering", 7: "error", 8: "connecting", 9: "skipping back", 10: "skipping forward", 11: "skipping"}

ACTIONS = ("look", "screenshot", "select", "key", "type", "click", "wait", "open", "apps", "media", "search", "settings", "install")

Picture = Literal["always", "auto", "never"]


class ComputerError(Exception):
    """What the model reads when an action fails."""


@dataclass
class Look:
    """The screen after an action: what is in front, whether the screen is
    on, the screen as elements, and a JPEG of it when one was taken.
    `hidden` is a screen the app keeps out of screenshots; `error` says
    where an action stopped partway, and `note` what a task did."""

    front: str | None
    screen: str
    elements: str | None = None
    jpeg: bytes | None = None
    size: tuple[int, int] = (0, 0)
    hidden: bool = False
    error: str | None = None
    note: str | None = None
    media: dict[str, Any] | None = None


@dataclass
class Outcome:
    result: dict[str, Any]
    image: str | None = None


class ExternalComputer(ABC):
    """A screen the model drives. Every action answers the screen after it.
    Coordinates are in the screenshot's pixels, and elements are numbered as
    the last look numbered them."""

    @abstractmethod
    async def look(self, picture: Picture) -> Look: ...

    @abstractmethod
    async def select(self, element: int) -> Look: ...

    @abstractmethod
    async def keys(self, names: list[str]) -> Look: ...

    @abstractmethod
    async def type(self, text: str) -> Look: ...

    @abstractmethod
    async def click(self, x: int, y: int) -> Look: ...

    @abstractmethod
    async def wait(self, seconds: float) -> Look: ...

    @abstractmethod
    async def open(self, target: str) -> Look: ...

    @abstractmethod
    async def apps(self) -> list[str]: ...

    @abstractmethod
    async def media(self, command: str, times: int) -> Look: ...

    @abstractmethod
    async def search(self, query: str) -> Look: ...

    @abstractmethod
    async def settings(self) -> Look: ...

    @abstractmethod
    async def install(self, package: str | None) -> Look: ...

    async def watch(self) -> None:
        """Runs for the kernel's life; a computer with nothing to watch
        returns at once."""

    async def close(self) -> None:
        return None


async def use(computer: ExternalComputer, action: str, args: dict[str, Any], shots: Path, now: datetime) -> Outcome:
    """Runs one action of the external_computer tool, and answers with the
    screen after it. A screenshot is saved under `shots` too, so the model
    can show it to its person."""
    result: dict[str, Any] = {"action": action}
    seen: Look | None = None
    if action == "look":
        seen = await computer.look("auto")
    elif action == "screenshot":
        seen = await computer.look("always")
    elif action == "select":
        element = args.get("element")
        if isinstance(element, bool) or not isinstance(element, int):
            raise ComputerError("select needs `element`: an element's number from the last look")
        seen = await computer.select(element)
        result["element"] = element
    elif action == "key":
        names = args.get("keys")
        if isinstance(names, str):
            names = names.split()
        if not names or not all(isinstance(n, str) for n in names):
            raise ComputerError("key needs `keys`: the names of the keys to press, in order")
        seen = await computer.keys(names)
        result["keys"] = names
    elif action == "type":
        text = args.get("text")
        if not isinstance(text, str) or not text:
            raise ComputerError("type needs `text`")
        seen = await computer.type(text)
    elif action == "click":
        x, y = _int(args, "x"), _int(args, "y")
        seen = await computer.click(x, y)
        result.update(x=x, y=y)
    elif action == "wait":
        seconds = args.get("seconds", 1)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            raise ComputerError("wait needs `seconds`, a number")
        seen = await computer.wait(min(float(seconds), MAX_WAIT))
    elif action == "open":
        target = args.get("target")
        if not isinstance(target, str) or not target.strip():
            raise ComputerError("open needs `target`: an app's package name, or a link")
        seen = await computer.open(target.strip())
        result["target"] = target.strip()
    elif action == "apps":
        result["apps"] = await computer.apps()
    elif action == "media":
        command = args.get("command", "state")
        if command not in MEDIA:
            raise ComputerError(f"media needs `command`: one of {', '.join(MEDIA)}")
        times = args.get("times", 1)
        if isinstance(times, bool) or not isinstance(times, int) or not 1 <= times <= 30:
            raise ComputerError("media's `times` is a whole number from 1 to 30")
        seen = await computer.media(command, times)
        result["command"] = command
    elif action == "search":
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ComputerError("search needs `query`")
        seen = await computer.search(query.strip())
        result["query"] = query.strip()
    elif action == "settings":
        seen = await computer.settings()
    elif action == "install":
        package = args.get("package")
        if package is not None and (not isinstance(package, str) or not PACKAGE.fullmatch(package.strip())):
            raise ComputerError("install's `package` is an app's package name; leave it out for the app whose Appstore page is in front")
        seen = await computer.install(package.strip() if package else None)
    else:
        raise ComputerError(f"no action {action!r}; the actions are {', '.join(ACTIONS)}")
    image = None
    if seen is not None:
        result["in_front"] = seen.front
        result["screen"] = seen.screen
        for name in ("error", "note", "media"):
            if getattr(seen, name):
                result[name] = getattr(seen, name)
        if seen.elements:
            result["elements"] = seen.elements
        if seen.hidden:
            result["screenshot"] = "none: this app hides its screen from screenshots"
        if seen.jpeg is not None:
            result["screenshot"] = str(_save(seen.jpeg, shots, now))
            result["screenshot_size"] = list(seen.size)
            image = base64.b64encode(seen.jpeg).decode()
    return Outcome(result, image)


def _int(args: dict[str, Any], name: str) -> int:
    value = args.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComputerError(f"click needs `{name}`, in the screenshot's pixels")
    return round(value)


def _save(jpeg: bytes, folder: Path, now: datetime) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{now:%Y%m%d-%H%M%S-%f}.jpg"
    path.write_bytes(jpeg)
    for old in sorted(folder.glob("*.jpg"))[:-KEEP_SHOTS]:
        old.unlink(missing_ok=True)
    return path


# The screen as elements


@dataclass
class Elements:
    """The screen as the model reads it, and what each number stands for:
    the node's identity, for the helper to find it again."""

    text: str
    numbered: dict[int, dict[str, Any]]


# Longest a line of text on the screen is shown.
LINE = 200


def elements(nodes: list[dict[str, Any]]) -> Elements:
    """One line per thing on the screen, in the tree's order. A node the
    remote can highlight or press is numbered, named by its own text or
    description or else by the text inside it, and marked when it has the
    highlight; text outside any of them is a plain line. A node that holds
    others the remote can reach is a list, not numbered itself. Runs of
    single characters (an on-screen keyboard's keys) share one line."""
    children: dict[int, list[int]] = {}
    for i, node in enumerate(nodes):
        children.setdefault(node.get("p", -1), []).append(i)

    def actionable(node: dict[str, Any]) -> bool:
        return bool(node.get("focusable") or node.get("clickable") or node.get("editable"))

    def named(node: dict[str, Any]) -> str:
        return (node.get("text") or node.get("desc") or "").strip()

    def inside(i: int) -> list[int]:
        found = []
        for child in children.get(i, []):
            found.append(child)
            found.extend(inside(child))
        return found

    used: set[int] = set()
    lines: list[str] = []
    singles: list[str] = []
    numbered: dict[int, dict[str, Any]] = {}

    def flush() -> None:
        if singles:
            lines.append("    " + " ".join(singles))
            singles.clear()

    for i, node in enumerate(nodes):
        if i in used:
            continue
        name = named(node)
        below = inside(i)
        if actionable(node) and any(actionable(nodes[j]) for j in below):
            # A list or a row of things that can be selected: they are
            # numbered themselves, and the text between them stays text.
            if name:
                flush()
                lines.append("    " + _short(name))
            continue
        if actionable(node):
            within = below
            used.update(within)
            label = name
            if not label:
                parts: list[str] = []
                for j in within:
                    part = named(nodes[j])
                    if part and part not in parts:
                        parts.append(part)
                label = " / ".join(parts[:3])
            highlighted = node.get("focused") or any(nodes[j].get("focused") for j in within)
            if not label and not highlighted:
                continue
            flush()
            number = len(numbered) + 1
            numbered[number] = {
                "class": node.get("class"),
                "text": node.get("text"),
                "desc": node.get("desc"),
                "id": node.get("id"),
                "bounds": node.get("bounds"),
            }
            marks = []
            if highlighted:
                marks.append("highlighted")
            if node.get("editable"):
                marks.append("text field")
            if node.get("selected"):
                marks.append("selected")
            if "checked" in node:
                marks.append("on" if node["checked"] else "off")
            if node.get("disabled"):
                marks.append("disabled")
            line = f"[{number}] {_short(label) or '(unnamed)'}"
            lines.append(line + (f" ({', '.join(marks)})" if marks else ""))
        elif name:
            if len(name) <= 2:
                singles.append(name)
                continue
            flush()
            lines.append("    " + _short(name))
    flush()
    return Elements("\n".join(lines), numbered)


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= LINE else text[: LINE - 1] + "…"


# The Fire TV over ADB

# How often `watch` tries the Stick while it is off, and checks the
# connection while it is on.
WATCH_EVERY = 10.0
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 15.0
# How long a helper call may take: its waits, and the look after.
HELPER_TIMEOUT = 60.0
# The helper, and where it goes on the box: named by its hash, so a new
# build never runs an old copy.
HELPER = Path(__file__).with_name("tv-helper.jar")
HELPER_DIR = "/data/local/tmp"
# How long the screen gets to settle after an action, before the look that
# answers it: a short wait for the app to take the input, then until the
# accessibility events stop, at most SETTLE_MOST. Video apps send none, so
# for them the short wait is what there is.
SETTLE = ["sleep:400", "idle:1500"]
# Opening an app: how long it may take to come to the front.
OPEN_WAIT = 10_000
# Fire OS 5's launcher, as read off the family's Stick on 2026-11-01.
LAUNCHER = "com.amazon.tv.launcher"
FIND = [{"label": "Find"}]
SEARCH = [{"id": "com.amazon.tv.launcher:id/search_btn_text"}, {"label": "Search"}]
SUGGESTION = "com.amazon.tv.launcher:id/title"
SEARCH_RESULTS = [{"id": "com.amazon.tv.launcher:id/search_text"}]
SETTINGS = [{"id": "com.amazon.tv.launcher:id/nav_bar_settings"}, {"label": "Settings"}]
# The Appstore: an app's page, and its button, whose label says what a press
# does ("Download You own it" for a free app the account has).
APPSTORE = "com.amazon.venezia"
BUY = "com.amazon.venezia:id/buy_button"
# A button that downloads at no cost starts with one of these words. Anything
# else (a price, "Buy", "Rent") is left alone: huvu never pays.
FREE = ("download", "get", "free", "install", "update")
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+")
COMPONENT = re.compile(rf"{PACKAGE.pattern}/[A-Za-z0-9_.$]+")
FOCUS = re.compile(r"mCurrentFocus=Window\{\S+ \S+ ([^ }]+)\}")
SIZE = re.compile(r"(Physical|Override) size: (\d+)x(\d+)")
WAKEFULNESS = re.compile(r"mWakefulness=(\w+)")
SECTION = re.compile(rb"HUVU (\w+) (\d+)\n")
STATE_MARK = "HUVU-STATE"
OPENED_MARK = "HUVU-OPENED"


@dataclass
class AdbComputer(ExternalComputer):
    """A Fire TV (or any Android) over ADB on the network, with huvu's key,
    which the Stick allowed once."""

    host: str
    port: int
    key: Path
    open_on_boot: str | None = None
    _device: Any = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _size: tuple[int, int] | None = field(default=None, init=False, repr=False)
    _boot: str | None = field(default=None, init=False, repr=False)
    _helper_on_box: bool = field(default=False, init=False, repr=False)
    _numbered: dict[int, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)

    # The device

    async def _connect(self) -> Any:
        from adb_shell.adb_device_async import AdbDeviceTcpAsync
        from adb_shell.auth.sign_pythonrsa import PythonRSASigner

        if not self.key.exists():
            raise ComputerError(f"huvu has no ADB key at {self.key}")
        signer = PythonRSASigner.FromRSAKeyPath(str(self.key))
        device = AdbDeviceTcpAsync(self.host, self.port, default_transport_timeout_s=READ_TIMEOUT)
        try:
            await device.connect(rsa_keys=[signer], transport_timeout_s=CONNECT_TIMEOUT, auth_timeout_s=10.0)
        except Exception as err:
            with contextlib.suppress(Exception):
                await device.close()
            raise ComputerError("the TV box doesn't answer: the home theatre's AV power may be off, or it is still starting") from err
        self._size = None
        self._helper_on_box = False
        return device

    async def _run(self, command: str, timeout: float = READ_TIMEOUT) -> str:
        """A shell command's output, as text."""
        out = await self._on_device(lambda device: device.shell(command, read_timeout_s=timeout))
        return out.replace("\r", "")

    async def _exec_out(self, command: str, timeout: float, again: bool = True) -> bytes:
        """A command's output as it came, for binary output: `shell` runs
        on a terminal, which would mangle it."""
        return await self._on_device(lambda device: device.exec_out(command, read_timeout_s=timeout, decode=False), again)

    async def _on_device(self, call: Any, again: bool = True) -> Any:
        """Runs a call on the device, connecting first if need be. A
        connection that dies under a call (the AV power was cut and came
        back) is made again once, unless `again` is off. A call that timed
        out isn't run again: it may have pressed keys already."""
        async with self._lock:
            out: Any = None
            for attempt in (1, 2):
                fresh = self._device is None
                if fresh:
                    self._device = await self._connect()
                try:
                    out = await call(self._device)
                    break
                except Exception as err:
                    log.warning("the TV box failed a call (attempt %d): %r", attempt, err)
                    await self._drop()
                    if fresh or attempt == 2 or not again or "timeout" in type(err).__name__.lower():
                        raise ComputerError("the TV box stopped answering") from err
            return out

    async def _drop(self) -> None:
        device, self._device = self._device, None
        if device is not None:
            with contextlib.suppress(Exception):
                await device.close()

    async def close(self) -> None:
        async with self._lock:
            await self._drop()

    async def _screen_size(self) -> tuple[int, int]:
        if self._size is None:
            found = {kind: (int(w), int(h)) for kind, w, h in SIZE.findall(await self._run("wm size"))}
            self._size = found.get("Override") or found.get("Physical") or (1920, 1080)
        return self._size

    async def _shot_size(self) -> tuple[int, int]:
        width, height = await self._screen_size()
        return SHOT_WIDTH, round(SHOT_WIDTH * height / width / 2) * 2

    # The helper

    async def _put_helper(self) -> str:
        """Puts this build of the helper on the box, once a connection."""
        path = _helper_path()
        if not self._helper_on_box:
            if path not in await self._run(f"ls {path} 2>/dev/null"):
                await self._run(f"rm -f {HELPER_DIR}/huvu-tv-*.jar")
                await self._on_device(lambda device: device.push(str(HELPER), path, read_timeout_s=READ_TIMEOUT))
                log.info("put the helper on the TV box at %s", path)
            self._helper_on_box = True
        return path

    async def _helper(self, steps: list[str], picture: Picture = "auto", before: str | None = None, after: str | None = None) -> tuple[Look, str, dict[str, bytes]]:
        """Runs `before` in the box's shell, the helper's steps and a look,
        then `after`, all in one command, and answers the look, what
        `before` and `after` printed, and the helper's other sections."""
        path = await self._put_helper()
        width, height = await self._shot_size()
        helper = " ".join(shlex.quote(s) for s in [*steps, f"look:{width}x{height},{picture}"])
        command = "; ".join(
            part
            for part in (
                before and f"{{ {before}; }} 2>&1",
                f"echo {STATE_MARK}",
                f"CLASSPATH={path} app_process /system/bin com.huvu.tvhelper.Helper {helper} 2>/dev/null",
                f"echo {STATE_MARK}",
                "dumpsys window windows | grep mCurrentFocus",
                "dumpsys power | grep mWakefulness=",
                after,
            )
            if part
        )
        raw = await self._exec_out(command, timeout=HELPER_TIMEOUT)
        mark = f"{STATE_MARK}\n".encode()
        printed, _, rest = raw.partition(mark)
        body, _, state = rest.partition(mark)
        sections = _sections(body)
        state_text = state.decode(errors="replace").replace("\r", "")
        if "tree" not in sections:
            problem = sections.get("error", b"").decode(errors="replace") or body.decode(errors="replace").strip()[:300]
            raise ComputerError(f"the TV box's helper failed: {problem or 'it printed nothing'}")
        found = FOCUS.search(state_text)
        wake = WAKEFULNESS.search(state_text)
        screen = {"Awake": "on", "Dreaming": "screensaver", "Dozing": "off", "Asleep": "off"}.get(wake.group(1) if wake else "", "unknown")
        tree = json.loads(sections["tree"])
        seen = elements(tree.get("nodes", []))
        self._numbered = seen.numbered
        jpeg = sections.get("shot") if screen != "off" else None
        look = Look(
            front=found.group(1) if found else None,
            screen=screen,
            elements=seen.text or None,
            jpeg=jpeg,
            size=(width, height) if jpeg else (0, 0),
            hidden="hidden" in sections and screen != "off",
            error=sections["error"].decode(errors="replace") if "error" in sections else None,
        )
        return look, printed.decode(errors="replace").replace("\r", "") + state_text, sections

    # What the model does

    async def front(self) -> str | None:
        found = FOCUS.search(await self._run("dumpsys window windows | grep mCurrentFocus"))
        return found.group(1) if found else None

    async def look(self, picture: Picture) -> Look:
        return (await self._helper([], picture))[0]

    async def select(self, element: int) -> Look:
        want = self._numbered.get(element)
        if want is None:
            raise ComputerError(f"no element {element} in the last look: look again")
        return (await self._helper([_select([want]), *SETTLE]))[0]

    async def keys(self, names: list[str]) -> Look:
        return (await self._helper([_keys(names), *SETTLE]))[0]

    async def type(self, text: str) -> Look:
        if not text.isascii() or not text.isprintable():
            raise ComputerError("only plain text without accents or symbols outside ASCII can be typed")
        return (await self._helper([_text(text), *SETTLE]))[0]

    async def click(self, x: int, y: int) -> Look:
        shot_width, shot_height = await self._shot_size()
        if not (0 <= x < shot_width and 0 <= y < shot_height):
            raise ComputerError(f"({x}, {y}) is off the screen, which is {shot_width}x{shot_height}")
        width, height = await self._screen_size()
        return (await self._helper([f"tap:{round(x * width / shot_width)},{round(y * height / shot_height)}", *SETTLE]))[0]

    async def wait(self, seconds: float) -> Look:
        return (await self._helper([f"sleep:{round(seconds * 1000)}"]))[0]

    async def open(self, target: str) -> Look:
        """A link opens in the app that takes it, a package/activity starts
        that activity, and a package opens as its launcher icon would, the
        TV's icon first."""
        steps = ["sleep:1500", "idle:2500"]
        if "://" in target:
            before = f"am start -a android.intent.action.VIEW -d {shlex.quote(target)} | grep -q Error || echo {OPENED_MARK}"
        elif COMPONENT.fullmatch(target):
            before = f"am start -n {target} | grep -q Error || echo {OPENED_MARK}"
        elif PACKAGE.fullmatch(target):
            before = " || ".join(
                f"monkey -p {target} -c android.intent.category.{c} 1 | grep -q 'Events injected: 1' && echo {OPENED_MARK}"
                for c in ("LEANBACK_LAUNCHER", "LAUNCHER")
            )
            steps = [f"front:{target},{OPEN_WAIT}", "idle:2500"]
        else:
            raise ComputerError("open needs an app's package name, a package/activity, or a link")
        look, printed, _ = await self._helper(steps, before=before)
        if OPENED_MARK not in printed:
            raise ComputerError(f"{target} didn't open: nothing installed takes it")
        return look

    async def apps(self) -> list[str]:
        out = await self._run("pm list packages -3")
        return sorted(line.removeprefix("package:").strip() for line in out.splitlines() if line.startswith("package:"))

    async def media(self, command: str, times: int) -> Look:
        code = MEDIA[command]
        steps = [] if code is None else [_keys_codes([code] * (times if command in ("forward", "back") else 1)), "sleep:800"]
        look, printed, _ = await self._helper(steps, after="dumpsys media_session; cat /proc/uptime")
        # An app that publishes no media session (Netflix doesn't) can't be
        # read: its state is unknown, not "nothing plays".
        look.media = playing(printed, look.front) or {"state": "unknown: the app in front doesn't say what it plays"}
        return look

    async def search(self, query: str) -> Look:
        """The box's own search, across its apps: Home, Find, Search, the
        query typed on its keyboard, and the suggestion that is the query,
        or else the first."""
        if not query.isascii() or not query.isprintable():
            raise ComputerError("only plain text without accents or symbols outside ASCII can be typed")
        suggestion = [{"id": SUGGESTION, "label": query}, {"id": SUGGESTION}]
        look, _, _ = await self._helper(
            [
                *_home(FIND),
                _select(FIND),
                _until(SEARCH, 6000),
                _select(SEARCH),
                # The keyboard's keys show in the tree only some of the
                # time, so the wait for it is a fixed one.
                "sleep:2000",
                _text(query),
                _until([{"id": SUGGESTION}], 8000),
                _select(suggestion),
                _until(SEARCH_RESULTS, 10000),
                "idle:2500",
            ]
        )
        return look

    async def settings(self) -> Look:
        """The box's settings: Home, then Settings, which opens its row."""
        return (await self._helper([*_home(SETTINGS), _select(SETTINGS), "idle:2500"]))[0]

    async def install(self, package: str | None) -> Look:
        """Downloads an app from the Appstore, from its page there: the one
        `package` opens, or the one in front. Only a free download is
        pressed."""
        before = None
        steps = [_until([{"id": BUY}], 8000), "idle:1500"]
        if package:
            before = f"am start -a android.intent.action.VIEW -d {shlex.quote('amzn://apps/android?p=' + package)}"
            steps = [f"front:{APPSTORE},{OPEN_WAIT}", *steps]
        look, _, sections = await self._helper(steps, picture="never", before=before)
        if look.error:
            return look
        button = _node(sections, BUY)
        label = (button.get("desc") or button.get("text") or "").strip() if button else ""
        if not button:
            look.error = "no Appstore page with a download button is in front"
            return look
        if label.lower().startswith("open"):
            look.note = f"already installed (the button says {label!r})"
            return look
        if not label.lower().startswith(FREE):
            look.error = f"not pressed: the button says {label!r}, which isn't a free download, and huvu never pays"
            return look
        pressed = (await self._helper([_select([{"id": BUY}]), "sleep:1500", "idle:2500"], picture="never"))[0]
        if not pressed.error:
            pressed.note = f"pressed {label!r}: it downloads and installs in the background, and `apps` lists it once it is in"
        return pressed

    # The Stick's boots

    async def watch(self) -> None:
        while True:
            try:
                await self._check()
            except ComputerError:
                pass
            except Exception:
                log.exception("watching the TV box failed")
            await asyncio.sleep(WATCH_EVERY)

    async def _check(self) -> None:
        """Connects when the Stick has come up, and opens huvu on its boot."""
        boot_id, uptime = (await self._run("cat /proc/sys/kernel/random/boot_id /proc/uptime")).split()[:2]
        if boot_id == self._boot:
            return
        self._boot = boot_id
        log.info("the TV box answers, up %.0f s", float(uptime))
        if self.open_on_boot and float(uptime) < BOOT_WINDOW:
            await self._open_huvu()

    async def _open_huvu(self) -> None:
        component = self.open_on_boot or ""
        package = component.split("/")[0]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BOOT_WAIT
        while await self.front() is None:
            if loop.time() > deadline:
                log.warning("the TV box showed no app %.0f s after it booted, so huvu wasn't opened", BOOT_WAIT)
                return
            await asyncio.sleep(BOOT_SETTLE)
        await asyncio.sleep(BOOT_SETTLE)
        for _ in range(OPEN_TRIES):
            await self._run(f"input keyevent {KEYS['wake']} && am start -n {component}")
            settle = loop.time() + BOOT_OPEN_WAIT
            while loop.time() < settle:
                if (await self.front() or "").startswith(package + "/"):
                    log.info("opened %s on the TV box", component)
                    return
                await asyncio.sleep(BOOT_SETTLE)
        log.warning("%s didn't come to the front on the TV box", component)


# A first connection this many seconds after the Stick booted counts as its
# boot, and opens huvu.
BOOT_WINDOW = 300.0
# How long the boot waits for an app in front, then how long the opened app
# gets to come to the front, and how many times it is opened.
BOOT_WAIT = 300.0
BOOT_SETTLE = 5.0
BOOT_OPEN_WAIT = 30.0
OPEN_TRIES = 3


@functools.cache
def _helper_path() -> str:
    digest = hashlib.sha256(HELPER.read_bytes()).hexdigest()[:12]
    return f"{HELPER_DIR}/huvu-tv-{digest}.jar"


def _sections(body: bytes) -> dict[str, bytes]:
    """The helper's answers: `HUVU <name> <length>\\n` and that many bytes
    each. Anything the runtime printed between them is skipped."""
    sections: dict[str, bytes] = {}
    at = 0
    while True:
        found = SECTION.search(body, at)
        if not found:
            return sections
        start = found.end()
        end = start + int(found.group(2))
        sections[found.group(1).decode()] = body[start:end]
        at = end


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _select(wants: list[dict[str, Any]]) -> str:
    return f"select:{_b64(json.dumps(wants))}"


def _text(text: str) -> str:
    return f"text:{_b64(text)}"


def _keys_codes(codes: list[int]) -> str:
    return "key:" + ",".join(str(c) for c in codes)


def _keys(names: list[str]) -> str:
    codes = []
    for name in names:
        code = KEYS.get(re.sub(r"[\s_\-]", "", name.lower()))
        if code is None:
            raise ComputerError(f"no key {name!r}; the keys are {', '.join(sorted(KEYS))}")
        codes.append(code)
    return _keys_codes(codes)


def _until(wants: list[dict[str, Any]], ms: int) -> str:
    return f"until:{_b64(json.dumps(wants))},{ms}"


def _home(there: list[dict[str, Any]]) -> list[str]:
    """Home, and a wait until `there` shows on it. Home goes twice: the
    first press may only wake the screen or end the screensaver, and the
    launcher takes a moment to start its transition, which `idle` would
    take for a screen at rest."""
    home = _keys_codes([KEYS["home"]])
    return [home, "sleep:1200", home, _until(there, 8000), "idle:1000"]


def _node(sections: dict[str, bytes], resource: str) -> dict[str, Any] | None:
    """The first node of the look's tree with that resource id, with the
    text inside it."""
    nodes = json.loads(sections["tree"]).get("nodes", [])
    for i, node in enumerate(nodes):
        if node.get("id") == resource:
            inner = [n.get("text") or n.get("desc") for n in nodes if n.get("p") == i]
            return {**node, "text": " ".join(filter(None, [node.get("text"), *inner])) or None}
    return None


def playing(dump: str, front: str | None) -> dict[str, Any] | None:
    """What plays, from `dumpsys media_session` and `/proc/uptime`: the
    session of the app in front, or else one that is playing or paused,
    with its position in seconds now."""
    uptime = re.search(r"^(\d+\.\d+) \d+\.\d+$", dump, re.MULTILINE)
    now_ms = float(uptime.group(1)) * 1000 if uptime else None
    sessions = []
    stack = dump.split("Sessions Stack")[0]
    for block in re.split(r"\n(?=\S[^\n]* \S+/\S+\n  ownerPid=)", stack):
        package = re.search(r"^\s+package=(\S+)", block, re.MULTILINE)
        state = re.search(r"state=PlaybackState \{state=(\d+), position=(-?\d+), buffered position=-?\d+, speed=([\d.]+), updated=(\d+)", block)
        if not package or not state:
            continue
        title = re.search(r"metadata:size=\d+, description=([^,\n]+)", block)
        sessions.append((package.group(1), int(state.group(1)), int(state.group(2)), float(state.group(3)), int(state.group(4)), title.group(1).strip() if title else None))
    in_front = (front or "").split("/")[0]
    chosen = next((s for s in sessions if s[0] == in_front), None) or next((s for s in sessions if s[1] in (2, 3, 6)), None)
    if chosen is None:
        return None
    package, state, position, speed, updated, title = chosen
    if state == 3 and now_ms is not None and updated > 0:
        position += int((now_ms - updated) * speed)
    answer: dict[str, Any] = {"app": package, "state": PLAYBACK.get(state, str(state)), "position_seconds": max(0, position) // 1000}
    if title and title != "null":
        answer["title"] = title
    return answer
