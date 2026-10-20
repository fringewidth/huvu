"""The external computer: a screen outside Hermes that the model sees and
drives, through the `external_computer` tool the huvu Hermes plugin adds.

Hermes has a computer of its own (its Bot Desktop, where its browser runs),
so this one is named external. The only one is the home theatre's Fire TV
Stick, worked over ADB (`AdbComputer`). The plugin posts each action to the
backend (`POST /api/computer`), the backend runs it here, and the plugin
hands the model the screenshot as an image, so the model sees the screen in
the same step. A session may use it only where it may use the browser: on
pro or advanced (`Kernel.may_browse`).

The Stick takes one ADB client at a time, so huvu holds the connection for
everything done to it, and nothing else may: Home Assistant's Android TV
integration was removed for it (2026-10-17). The Stick sits on the AV power
and is off most of the day. `watch` tries it every WATCH_EVERY while it is
off, and checks the connection while it is on. On the first connection
after the Stick booted, it opens huvu's TV app (`open_on_boot`), the way
Home Assistant's `home_theatre_huvu` used to: ADB answers about 26 s after
power, before the home screen, which would cover an app opened then, so it
waits for an app in front first.

The Stick's adbd is Android 5.1's: it sends 4 KB a packet and waits for an
acknowledgment after each, so it moves about 160 KB/s over the home Wi-Fi
whatever the link's speed. A PNG of the screen is 1.3 MB, 9 to 11 s
(measured 2026-10-17). So a screenshot is one second of `screenrecord` at
the size the model sees, about 36 KB, and ffmpeg takes its last frame:
about 3 s in all. In the first minutes after the Stick boots,
`screenrecord` can hang (seen on 2026-10-18 and 2026-10-20, up to 7
minutes after boot), so it gets RECORD_WAIT, and then the screenshot is a
`screencap` PNG. `input` starts a Java VM on every call, about a second,
so keys pressed together go in one call.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import re
import shlex
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("huvu.external_computer")

# The size of the screenshots the model sees, and of the space its clicks
# are in, whatever the screen's own size.
SHOT_WIDTH = 1280
# How long the screen gets to settle after an action, before the screenshot
# that answers it. Opening an app takes longer.
SETTLE = 1.0
SETTLE_OPEN = 3.0
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

ACTIONS = ("screenshot", "click", "key", "type", "open", "apps", "wait")


class ComputerError(Exception):
    """What the model reads when an action fails."""


@dataclass
class Look:
    """The screen after an action: what is in front, whether the screen is
    on, and a JPEG of it when it is."""

    front: str | None
    screen: str
    jpeg: bytes | None = None
    size: tuple[int, int] = (0, 0)


@dataclass
class Outcome:
    result: dict[str, Any]
    image: str | None = None


class ExternalComputer(ABC):
    """A screen the model drives. Coordinates are in the screenshot's
    pixels."""

    @abstractmethod
    async def look(self) -> Look: ...

    @abstractmethod
    async def click(self, x: int, y: int) -> None: ...

    @abstractmethod
    async def keys(self, names: list[str]) -> None: ...

    @abstractmethod
    async def type(self, text: str) -> None: ...

    @abstractmethod
    async def open(self, target: str) -> None: ...

    @abstractmethod
    async def apps(self) -> list[str]: ...

    async def watch(self) -> None:
        """Runs for the kernel's life; a computer with nothing to watch
        returns at once."""

    async def close(self) -> None:
        return None


async def use(computer: ExternalComputer, action: str, args: dict[str, Any], shots: Path, now: datetime) -> Outcome:
    """Runs one action of the external_computer tool, and answers with the
    screen after it. The screenshot is saved under `shots` too, so the
    model can show it to its person."""
    result: dict[str, Any] = {"action": action}
    settle = SETTLE
    looks = action != "apps"
    if action == "screenshot":
        settle = 0
    elif action == "click":
        x, y = _int(args, "x"), _int(args, "y")
        await computer.click(x, y)
        result.update(x=x, y=y)
    elif action == "key":
        names = args.get("keys")
        if isinstance(names, str):
            names = names.split()
        if not names or not all(isinstance(n, str) for n in names):
            raise ComputerError("key needs `keys`: the names of the keys to press, in order")
        await computer.keys(names)
        result["keys"] = names
    elif action == "type":
        text = args.get("text")
        if not isinstance(text, str) or not text:
            raise ComputerError("type needs `text`")
        await computer.type(text)
    elif action == "open":
        target = args.get("target")
        if not isinstance(target, str) or not target.strip():
            raise ComputerError("open needs `target`: an app's package name, or a link")
        await computer.open(target.strip())
        result["target"] = target.strip()
        settle = SETTLE_OPEN
    elif action == "apps":
        result["apps"] = await computer.apps()
    elif action == "wait":
        seconds = args.get("seconds", SETTLE)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            raise ComputerError("wait needs `seconds`, a number")
        settle = min(float(seconds), MAX_WAIT)
    else:
        raise ComputerError(f"no action {action!r}; the actions are {', '.join(ACTIONS)}")
    image = None
    if looks:
        await asyncio.sleep(settle)
        seen: Look | None = None
        try:
            seen = await computer.look()
        except ComputerError as err:
            # The action itself ran: the model must know it did, even
            # when it can't see what came of it.
            if action in ("screenshot", "wait"):
                raise
            result["screen"] = f"unseen: {err}"
        if seen is not None:
            result["in_front"] = seen.front
            result["screen"] = seen.screen
        if seen is not None and seen.jpeg is not None:
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


# How often `watch` tries the Stick while it is off, and checks the
# connection while it is on.
WATCH_EVERY = 10.0
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 15.0
# How long a one-second `screenrecord` may take before the screenshot is a
# `screencap` instead, and how long that may take.
RECORD_WAIT = 8.0
SCREENCAP_WAIT = 60.0
# A first connection this many seconds after the Stick booted counts as its
# boot, and opens huvu.
BOOT_WINDOW = 300.0
# How long the boot waits for an app in front, then how long the opened app
# gets to come to the front, and how many times it is opened.
BOOT_WAIT = 300.0
BOOT_SETTLE = 5.0
OPEN_WAIT = 30.0
OPEN_TRIES = 3
RECORDING = "/sdcard/huvu-screen.mp4"
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+")
COMPONENT = re.compile(rf"{PACKAGE.pattern}/[A-Za-z0-9_.$]+")
FOCUS = re.compile(r"mCurrentFocus=Window\{\S+ \S+ ([^ }]+)\}")
SIZE = re.compile(r"(Physical|Override) size: (\d+)x(\d+)")
WAKEFULNESS = re.compile(r"mWakefulness=(\w+)")


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
        back) is made again once, unless `again` is off."""
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
                    if fresh or attempt == 2 or not again:
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

    # What the model does

    async def front(self) -> str | None:
        found = FOCUS.search(await self._run("dumpsys window windows | grep mCurrentFocus"))
        return found.group(1) if found else None

    async def look(self) -> Look:
        front = await self.front()
        found = WAKEFULNESS.search(await self._run("dumpsys power | grep mWakefulness="))
        screen = {"Awake": "on", "Dreaming": "screensaver", "Dozing": "off", "Asleep": "off"}.get(found.group(1) if found else "", "unknown")
        jpeg, size = None, (0, 0)
        if screen != "off":
            size = await self._shot_size()
            jpeg = await self._screenshot(*size)
        return Look(front, screen, jpeg, size)

    async def _screenshot(self, width: int, height: int) -> bytes:
        # A recording that hangs isn't tried again: screencap follows.
        video = b""
        with contextlib.suppress(ComputerError):
            video = await self._exec_out(
                f"screenrecord --size {width}x{height} --bit-rate 4000000 --time-limit 1 {RECORDING} >/dev/null 2>&1; cat {RECORDING}; rm -f {RECORDING}",
                timeout=RECORD_WAIT,
                again=False,
            )
        frame = None
        if video:
            with contextlib.suppress(ComputerError):
                frame = await last_frame(video, width, height)
        if frame is None:
            # screenrecord fails on some screens; screencap always works,
            # at about 10 s.
            frame = await last_frame(await self._exec_out("screencap -p", timeout=SCREENCAP_WAIT), width, height)
        return frame

    async def click(self, x: int, y: int) -> None:
        shot_width, shot_height = await self._shot_size()
        if not (0 <= x < shot_width and 0 <= y < shot_height):
            raise ComputerError(f"({x}, {y}) is off the screen, which is {shot_width}x{shot_height}")
        width, height = await self._screen_size()
        await self._run(f"input tap {round(x * width / shot_width)} {round(y * height / shot_height)}")

    async def keys(self, names: list[str]) -> None:
        codes = []
        for name in names:
            code = KEYS.get(re.sub(r"[\s_\-]", "", name.lower()))
            if code is None:
                raise ComputerError(f"no key {name!r}; the keys are {', '.join(sorted(KEYS))}")
            codes.append(code)
        await self._run("input keyevent " + " ".join(str(c) for c in codes))

    async def type(self, text: str) -> None:
        if not text.isascii() or not text.isprintable():
            raise ComputerError("only plain text without accents or symbols outside ASCII can be typed")
        # `input text` reads %s as a space, and takes no space itself.
        await self._run("input text " + shlex.quote(text.replace(" ", "%s")))

    async def open(self, target: str) -> None:
        """A link opens in the app that takes it, a package/activity
        starts that activity, and a package opens as its launcher icon
        would, the TV's icon first."""
        started = lambda out: "Error" not in out
        if "://" in target:
            tries = [(f"am start -a android.intent.action.VIEW -d {shlex.quote(target)}", started)]
        elif COMPONENT.fullmatch(target):
            tries = [(f"am start -n {target}", started)]
        elif PACKAGE.fullmatch(target):
            injected = lambda out: "Events injected: 1" in out
            tries = [(f"monkey -p {target} -c android.intent.category.{c} 1", injected) for c in ("LEANBACK_LAUNCHER", "LAUNCHER")]
        else:
            raise ComputerError("open needs an app's package name, a package/activity, or a link")
        for command, worked in tries:
            if worked(await self._run(command)):
                return
        raise ComputerError(f"{target} didn't open: nothing installed takes it")

    async def apps(self) -> list[str]:
        out = await self._run("pm list packages -3")
        return sorted(line.removeprefix("package:").strip() for line in out.splitlines() if line.startswith("package:"))

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
            settle = loop.time() + OPEN_WAIT
            while loop.time() < settle:
                if (await self.front() or "").startswith(package + "/"):
                    log.info("opened %s on the TV box", component)
                    return
                await asyncio.sleep(BOOT_SETTLE)
        log.warning("%s didn't come to the front on the TV box", component)


async def last_frame(media: bytes, width: int, height: int) -> bytes:
    """The last frame of a video, or an image, as a JPEG of the given size,
    through ffmpeg. MP4 keeps its index at the end, so it goes through a
    file."""
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "in"
        target = Path(folder) / "out.jpg"
        source.write_bytes(media)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-y", "-i", str(source), "-vf", f"scale={width}:{height}", "-update", "1", "-q:v", "4", str(target),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await process.communicate()
        if process.returncode != 0 or not target.exists():
            raise ComputerError(f"the screenshot couldn't be read: {err.decode(errors='replace').strip()[:200]}")
        return target.read_bytes()
