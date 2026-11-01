"""The external computer: the tool's actions, the screen as elements, the
Fire TV over a fake ADB device running a fake helper, the backend's route
and the plugin's tool."""

from __future__ import annotations

import base64
import json
import shlex
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import uvicorn

from huvu import external_computer
from huvu.config import Config
from huvu.external_computer import AdbComputer, ComputerError, ExternalComputer, Look, Picture, elements, playing, use
from huvu.http import create_app
from huvu.kernel import Kernel
from huvu.prompts import COMPUTER_BLOCKED

from .fakes import FakeHermes
from .test_hermes_plugin import load as load_plugin

NOW = datetime(2026, 11, 1, 10, 0)
JPEG = b"\xff\xd8 a screenshot"
NETFLIX = "com.netflix.ninja/com.netflix.ninja.MainActivity"


class FakeComputer(ExternalComputer):
    def __init__(self, screen: str = "on", jpeg: bytes | None = JPEG, hidden: bool = False) -> None:
        self.screen = screen
        self.jpeg = jpeg
        self.hidden = hidden
        self.did: list[tuple] = []

    def _look(self, error: str | None = None, note: str | None = None) -> Look:
        jpeg = None if self.screen == "off" else self.jpeg
        return Look(NETFLIX, self.screen, "[1] Play (highlighted)", jpeg, (1280, 720) if jpeg else (0, 0), self.hidden, error, note)

    async def look(self, picture: Picture) -> Look:
        self.did.append(("look", picture))
        return self._look()

    async def select(self, element: int) -> Look:
        self.did.append(("select", element))
        return self._look()

    async def keys(self, names: list[str]) -> Look:
        self.did.append(("keys", *names))
        return self._look()

    async def type(self, text: str) -> Look:
        self.did.append(("type", text))
        return self._look()

    async def click(self, x: int, y: int) -> Look:
        self.did.append(("click", x, y))
        return self._look()

    async def wait(self, seconds: float) -> Look:
        self.did.append(("wait", seconds))
        return self._look()

    async def open(self, target: str) -> Look:
        self.did.append(("open", target))
        return self._look()

    async def apps(self) -> list[str]:
        return ["com.huvu.tv", "com.netflix.ninja"]

    async def media(self, command: str, times: int) -> Look:
        self.did.append(("media", command, times))
        look = self._look()
        look.media = {"state": "playing"}
        return look

    async def search(self, query: str) -> Look:
        self.did.append(("search", query))
        return self._look()

    async def settings(self) -> Look:
        self.did.append(("settings",))
        return self._look()

    async def install(self, package: str | None) -> Look:
        self.did.append(("install", package))
        return self._look(error="not pressed: the button says '₹99', which isn't a free download, and huvu never pays")


async def test_an_action_answers_with_the_screen_after_it(tmp_path):
    computer = FakeComputer()
    outcome = await use(computer, "key", {"keys": ["Down", "Down", "Return"]}, tmp_path, NOW)
    assert computer.did == [("keys", "Down", "Down", "Return")]
    assert outcome.result["in_front"] == NETFLIX and outcome.result["screen"] == "on"
    assert outcome.result["elements"] == "[1] Play (highlighted)"
    assert outcome.result["screenshot_size"] == [1280, 720]
    # The screenshot is kept, for the model to show its person.
    assert Path(outcome.result["screenshot"]).read_bytes() == JPEG
    assert base64.b64decode(outcome.image) == JPEG


async def test_a_look_without_a_screenshot_is_text_alone(tmp_path):
    outcome = await use(FakeComputer(jpeg=None), "look", {}, tmp_path, NOW)
    assert outcome.image is None and "screenshot" not in outcome.result
    assert outcome.result["elements"] == "[1] Play (highlighted)"
    hidden = await use(FakeComputer(jpeg=None, hidden=True), "screenshot", {}, tmp_path, NOW)
    assert hidden.result["screenshot"] == "none: this app hides its screen from screenshots" and hidden.image is None


async def test_look_and_screenshot_differ_in_the_picture_only(tmp_path):
    computer = FakeComputer()
    await use(computer, "look", {}, tmp_path, NOW)
    await use(computer, "screenshot", {}, tmp_path, NOW)
    assert computer.did == [("look", "auto"), ("look", "always")]


async def test_tasks_answer_where_they_stopped(tmp_path):
    computer = FakeComputer()
    paid = await use(computer, "install", {"package": "com.example.paid"}, tmp_path, NOW)
    assert "huvu never pays" in paid.result["error"] and paid.result["elements"]
    played = await use(computer, "media", {"command": "forward", "times": 3}, tmp_path, NOW)
    assert played.result["media"] == {"state": "playing"} and played.result["command"] == "forward"
    await use(computer, "search", {"query": " dune "}, tmp_path, NOW)
    await use(computer, "settings", {}, tmp_path, NOW)
    await use(computer, "install", {}, tmp_path, NOW)
    assert computer.did[1:] == [("media", "forward", 3), ("search", "dune"), ("settings",), ("install", None)]


async def test_apps_and_a_screen_that_is_off_have_no_screenshot(tmp_path):
    apps = await use(FakeComputer(), "apps", {}, tmp_path, NOW)
    assert apps.result == {"action": "apps", "apps": ["com.huvu.tv", "com.netflix.ninja"]} and apps.image is None
    off = await use(FakeComputer(screen="off"), "screenshot", {}, tmp_path, NOW)
    assert off.result["screen"] == "off" and "screenshot" not in off.result and off.image is None


async def test_actions_check_their_arguments(tmp_path):
    computer = FakeComputer()
    for action, args in [
        ("click", {"x": 10}),
        ("key", {}),
        ("type", {"text": ""}),
        ("open", {}),
        ("wait", {"seconds": "soon"}),
        ("select", {"element": "4"}),
        ("select", {"element": True}),
        ("media", {"command": "skip"}),
        ("media", {"command": "forward", "times": 0}),
        ("search", {"query": "  "}),
        ("install", {"package": "netflix; reboot"}),
        ("scroll", {}),
    ]:
        with pytest.raises(ComputerError):
            await use(computer, action, args, tmp_path, NOW)
    assert computer.did == []
    # Keys may come as one string too.
    await use(computer, "key", {"keys": "Up Return"}, tmp_path, NOW)
    assert computer.did == [("keys", "Up", "Return")]


async def test_only_the_latest_screenshots_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(external_computer, "KEEP_SHOTS", 2)
    for second in range(4):
        await use(FakeComputer(), "screenshot", {}, tmp_path, NOW.replace(second=second))
    assert len(list(tmp_path.glob("*.jpg"))) == 2


# The screen as elements, from trees shaped as the Stick's launcher gives
# them (2026-11-01).


def node(p: int, cls: str = "android.view.View", **rest) -> dict:
    return {"p": p, "class": cls, "bounds": [0, 0, 10, 10], **rest}


HOME = [
    node(-1, "android.widget.FrameLayout"),
    node(0, "android.widget.TextView", text="Learn more"),
    # The row of the nav bar: a list that can take the highlight itself.
    node(0, id="com.amazon.tv.launcher:id/menu_apps_gridview", focusable=True),
    node(2, desc="Find", focusable=True, clickable=True),
    node(3, "android.widget.ImageView"),
    node(2, desc="Home", focusable=True, clickable=True, focused=True),
    node(5, "android.widget.TextView", text="Home"),
    # A tile named only by the text inside it.
    node(0, "android.widget.FrameLayout", focusable=True, clickable=True),
    node(7, "android.widget.TextView", text="Network", id="com.amazon.tv.launcher:id/title_text"),
    node(0, "android.widget.Switch", text="Subtitles", focusable=True, checked=False),
    node(0, "android.widget.EditText", text="dune", editable=True, focusable=True),
    node(0, "android.widget.Button", text="Buy", focusable=True, disabled=True),
    # An unnamed, unhighlighted node that can be selected says nothing.
    node(0, "android.widget.FrameLayout", focusable=True),
    # An on-screen keyboard's keys.
    *[node(0, "AmazonKeyboard$Key", desc=k) for k in "ABC"],
    node(0, "android.widget.TextView", text="A very long description " * 20),
]


def test_elements_number_what_the_remote_can_reach():
    seen = elements(HOME)
    assert seen.text.splitlines() == [
        "    Learn more",
        "[1] Find",
        "[2] Home (highlighted)",
        "[3] Network",
        "[4] Subtitles (off)",
        "[5] dune (text field)",
        "[6] Buy (disabled)",
        "    A B C",
        "    " + " ".join(("A very long description " * 20).split())[:199] + "…",
    ]
    # Each number is the node's identity, for the helper to find it again.
    assert seen.numbered[2] == {"class": "android.view.View", "text": None, "desc": "Home", "id": None, "bounds": [0, 0, 10, 10]}
    assert seen.numbered[3]["class"] == "android.widget.FrameLayout"


def test_an_empty_tree_has_no_elements():
    assert elements([]).text == "" and elements([node(-1, "android.view.SurfaceView")]).text == ""


# The Fire TV over ADB, with a fake helper


class FakeDevice:
    """Answers what AdbComputer sends: shell commands, and the one command
    each action is, whose helper steps it records and answers."""

    def __init__(self, tv: "FakeTV") -> None:
        self.tv = tv

    async def shell(self, command: str, read_timeout_s: float = 10.0) -> str:
        self.tv.check()
        self.tv.commands.append(command)
        return self.tv.answer(command)

    async def exec_out(self, command: str, read_timeout_s: float = 10.0, decode: bool = True) -> bytes:
        self.tv.check()
        self.tv.commands.append(command)
        return self.tv.run(command)

    async def push(self, local: str, remote: str, read_timeout_s: float = 10.0) -> None:
        self.tv.check()
        self.tv.pushed.append(remote)
        self.tv.files.add(remote)

    async def close(self) -> None:
        self.tv.closed += 1


class FakeTV:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.steps: list[list[str]] = []
        self.pushed: list[str] = []
        self.files: set[str] = set()
        self.connects = 0
        self.closed = 0
        self.dead = False
        self.hangs = False
        self.off = False
        self.front = "com.amazon.tv.launcher/.ui.HomeActivity_vNext"
        self.wake = "Awake"
        self.tree: list[dict] = HOME
        self.shot: bytes | None = JPEG
        self.error: str | None = None
        self.uptime = 40.0
        self.boot = "boot-1"
        self.installed = {"com.netflix.ninja": "LEANBACK_LAUNCHER", "com.ghisler.android.TotalCommander": "LAUNCHER"}
        self.media_dump = ""

    def check(self) -> None:
        if self.dead:
            raise ConnectionResetError("the TV lost power")
        if self.hangs:
            raise TimeoutError("TcpTimeoutException: reading timed out")

    def answer(self, command: str) -> str:
        if command == "wm size":
            return "Physical size: 1920x1080\r\n"
        if command.startswith("ls "):
            path = command.split()[1]
            return f"{path}\r\n" if path in self.files else ""
        if command.startswith("rm -f"):
            self.files.clear()
            return ""
        if command.startswith("dumpsys window"):
            return f"  mCurrentFocus=Window{{24e92aae u0 {self.front}}}\r\n" if self.front else "  mCurrentFocus=null\r\n"
        if command.startswith("cat /proc/sys/kernel/random/boot_id"):
            return f"{self.boot}\r\n{self.uptime} 12.0\r\n"
        if "am start -n com.huvu.tv" in command:
            self.front = "com.huvu.tv/.MainActivity"
            return "Starting: Intent { cmp=com.huvu.tv/.MainActivity }\r\n"
        if command.startswith("pm list packages"):
            return "package:com.netflix.ninja\r\npackage:com.huvu.tv\r\n"
        return ""

    def run(self, command: str) -> bytes:
        """The action's command: what `before` printed, the helper's
        sections, then the state and what `after` printed."""
        printed = ""
        if command.startswith("{ "):
            before = command[2 : command.index("; } 2>&1")]
            printed = self.before(before)
        helper = command[command.index("com.huvu.tvhelper.Helper ") + len("com.huvu.tvhelper.Helper ") : command.index(" 2>/dev/null")]
        steps = shlex.split(helper)
        self.steps.append(steps)
        body = b""
        if self.error:
            body += section("error", self.error.encode())
        body += section("tree", json.dumps({"package": self.front.split("/")[0], "nodes": self.tree}).encode())
        picture = steps[-1].rsplit(",", 1)[1]
        if picture == "always" or (picture == "auto" and not any(n.get("text") or n.get("desc") for n in self.tree)):
            body += section("shot", self.shot) if self.shot else section("hidden", b"")
        state = f"  mCurrentFocus=Window{{24e92aae u0 {self.front}}}\n  mWakefulness={self.wake}\n"
        if "dumpsys media_session" in command:
            state += self.media_dump + "4000.00 3000.00\n"
        return printed.encode() + b"HUVU-STATE\n" + body + b"HUVU-STATE\n" + state.encode()

    def before(self, command: str) -> str:
        if command.startswith("monkey"):
            for part in command.split(" || "):
                package, category = part.split()[2], part.split()[4].rsplit(".", 1)[1]
                if self.installed.get(package) == category:
                    self.front = f"{package}/.MainActivity"
                    return "HUVU-OPENED\n"
            return ""
        if "amzn://apps" in command:
            self.front = "com.amazon.venezia/com.amazon.venezia.details.AppDetailsActivity"
            return ""
        if command.startswith("am start"):
            return "" if "missing" in command else "HUVU-OPENED\n"
        return ""


def section(name: str, body: bytes) -> bytes:
    return f"HUVU {name} {len(body)}\n".encode() + body


class FakeAdbComputer(AdbComputer):
    def __init__(self, tv: FakeTV, open_on_boot: str | None = None) -> None:
        super().__init__("192.168.29.7", 5555, Path("adbkey"), open_on_boot)
        self.tv = tv

    async def _connect(self) -> FakeDevice:
        if self.tv.off:
            raise ComputerError("the TV box doesn't answer")
        self.tv.connects += 1
        self._size = None
        self._helper_on_box = False
        return FakeDevice(self.tv)


def decoded(step: str) -> object:
    """A step's base64 argument, decoded."""
    name, _, value = step.partition(":")
    raw = base64.b64decode(value.split(",")[0]).decode()
    return json.loads(raw) if name in ("select", "until") else raw


async def test_the_helper_goes_on_the_box_once_a_connection_named_by_its_build():
    tv = FakeTV()
    computer = FakeAdbComputer(tv)
    await computer.look("auto")
    await computer.look("auto")
    path = external_computer._helper_path()
    assert tv.pushed == [path] and path.startswith("/data/local/tmp/huvu-tv-")
    assert f"CLASSPATH={path} app_process" in tv.commands[-1]
    # A new connection checks, and finds it there.
    computer._device = None
    await computer.look("auto")
    assert tv.pushed == [path]


async def test_a_look_is_the_tree_as_elements_and_a_screenshot_only_where_it_says_little():
    tv = FakeTV()
    seen = await FakeAdbComputer(tv).look("auto")
    assert seen.front == "com.amazon.tv.launcher/.ui.HomeActivity_vNext" and seen.screen == "on"
    assert seen.elements.splitlines()[2] == "[2] Home (highlighted)"
    assert seen.jpeg is None and tv.steps[-1] == ["look:1280x720,auto"]
    # A video app's screen: no tree to speak of, so a screenshot.
    tv.tree = [node(-1, "android.view.SurfaceView")]
    seen = await FakeAdbComputer(tv).look("auto")
    assert seen.jpeg == JPEG and seen.size == (1280, 720) and seen.elements is None
    # Netflix keeps its screen out of screenshots.
    tv.shot = None
    seen = await FakeAdbComputer(tv).look("always")
    assert seen.jpeg is None and seen.hidden


async def test_a_screen_that_is_off_or_dreaming_says_so():
    tv = FakeTV()
    tv.wake = "Dreaming"
    assert (await FakeAdbComputer(tv).look("auto")).screen == "screensaver"
    tv.wake = "Asleep"
    seen = await FakeAdbComputer(tv).look("always")
    assert seen.screen == "off" and seen.jpeg is None


async def test_keys_go_as_android_key_codes_then_settle():
    tv = FakeTV()
    computer = FakeAdbComputer(tv)
    await computer.keys(["Down", "arrow-down", "Return", "Escape", "Play_Pause"])
    assert tv.steps[-1] == ["key:20,20,23,4,85", *external_computer.SETTLE, "look:1280x720,auto"]
    with pytest.raises(ComputerError, match="no key 'Ctrl'"):
        await computer.keys(["Down", "Ctrl"])
    assert len(tv.steps) == 1


async def test_select_finds_the_element_the_last_look_numbered():
    tv = FakeTV()
    computer = FakeAdbComputer(tv)
    with pytest.raises(ComputerError, match="no element 2 in the last look"):
        await computer.select(2)
    await computer.look("auto")
    await computer.select(2)
    select = tv.steps[-1][0]
    assert select.startswith("select:")
    assert decoded(select) == [{"class": "android.view.View", "text": None, "desc": "Home", "id": None, "bounds": [0, 0, 10, 10]}]
    with pytest.raises(ComputerError, match="no element 99"):
        await computer.select(99)


async def test_clicks_are_scaled_from_the_screenshot_to_the_screen():
    tv = FakeTV()
    computer = FakeAdbComputer(tv)
    await computer.click(640, 360)
    assert tv.steps[-1][0] == "tap:960,540"
    with pytest.raises(ComputerError, match="off the screen"):
        await computer.click(1280, 10)


async def test_typing_sends_the_text_as_it_is():
    tv = FakeTV()
    computer = FakeAdbComputer(tv)
    await computer.type("the dark knight's")
    assert decoded(tv.steps[-1][0]) == "the dark knight's"
    with pytest.raises(ComputerError):
        await computer.type("café")


async def test_open_takes_a_package_a_component_or_a_link():
    tv = FakeTV()
    computer = FakeAdbComputer(tv)
    seen = await computer.open("com.netflix.ninja")
    assert "monkey -p com.netflix.ninja -c android.intent.category.LEANBACK_LAUNCHER 1" in tv.commands[-1]
    # It waits for the app to come to the front, then looks.
    assert tv.steps[-1][0] == f"front:com.netflix.ninja,{external_computer.OPEN_WAIT}" and seen.front == "com.netflix.ninja/.MainActivity"
    # An app with only a phone's launcher icon.
    await computer.open("com.ghisler.android.TotalCommander")
    assert tv.front == "com.ghisler.android.TotalCommander/.MainActivity"
    await computer.open("https://www.youtube.com/watch?v=abc")
    assert "am start -a android.intent.action.VIEW -d 'https://www.youtube.com/watch?v=abc'" in tv.commands[-1]
    with pytest.raises(ComputerError, match="didn't open"):
        await computer.open("com.example.missing")
    with pytest.raises(ComputerError, match="didn't open"):
        await computer.open("com.example/.missing")
    with pytest.raises(ComputerError, match="package name"):
        await computer.open("netflix; reboot")


async def test_search_goes_home_finds_search_types_and_picks_the_query():
    tv = FakeTV()
    await FakeAdbComputer(tv).search("dune")
    steps = tv.steps[-1]
    names = [s.split(":")[0] for s in steps]
    assert names == ["key", "sleep", "key", "until", "idle", "select", "until", "select", "sleep", "text", "until", "select", "until", "idle", "look"]
    assert decoded(steps[5]) == external_computer.FIND and decoded(steps[9]) == "dune"
    # The suggestion that is the query, or else the first.
    assert decoded(steps[11]) == [{"id": external_computer.SUGGESTION, "label": "dune"}, {"id": external_computer.SUGGESTION}]


async def test_settings_goes_home_and_selects_settings():
    tv = FakeTV()
    await FakeAdbComputer(tv).settings()
    assert decoded(tv.steps[-1][5]) == external_computer.SETTINGS


def store_page(button: str) -> list[dict]:
    return [
        node(-1, "android.widget.FrameLayout"),
        node(0, "android.widget.TextView", text="Spotify - Music and Podcasts"),
        node(0, "android.widget.ImageButton", desc=button, id=external_computer.BUY, focusable=True, focused=True),
    ]


async def test_install_presses_only_a_free_download():
    tv = FakeTV()
    tv.tree = store_page("Download You own it")
    computer = FakeAdbComputer(tv)
    seen = await computer.install("com.spotify.tv.android")
    assert "amzn://apps/android?p=com.spotify.tv.android" in tv.commands[-2]
    assert tv.steps[-2][0] == f"front:com.amazon.venezia,{external_computer.OPEN_WAIT}"
    assert decoded(tv.steps[-1][0]) == [{"id": external_computer.BUY}]
    assert "pressed 'Download You own it'" in seen.note and seen.error is None
    # A price is never pressed.
    tv.steps.clear()
    tv.tree = store_page("₹ 99.00 Buy")
    seen = await computer.install(None)
    assert len(tv.steps) == 1 and "huvu never pays" in seen.error
    # An app already there.
    tv.tree = store_page("Open")
    seen = await computer.install(None)
    assert seen.note == "already installed (the button says 'Open')"
    # No page with the button in front.
    tv.tree = HOME
    seen = await computer.install(None)
    assert seen.error == "no Appstore page with a download button is in front"


async def test_a_step_that_failed_says_where_and_the_screen_still_shows():
    tv = FakeTV()
    tv.error = "step 6 (select): no such element on the screen: Find"
    seen = await FakeAdbComputer(tv).search("dune")
    assert seen.error == tv.error and seen.elements


# What the Stick's `dumpsys media_session` printed on 2026-11-01, with a
# session of a playing app added the same way.
MEDIA_DUMP = """MEDIA SESSION SERVICE (dumpsys media_session)

3 sessions listeners.
2 Sessions:
AlexaMediaPlayerRuntime com.amazon.alexamediaplayer.runtime.ftv/AlexaMediaPlayerRuntime
  ownerPid=6990, ownerUid=32056, userId=0
  package=com.amazon.alexamediaplayer.runtime.ftv
  active=false
  state=PlaybackState {state=0, position=0, buffered position=0, speed=0.0, updated=0, actions=6, custom actions=[], active item id=-1, error=null}
  metadata:size=1, description=null, null, null
YouTube com.amazon.firetv.youtube/YouTube
  ownerPid=7001, ownerUid=10050, userId=0
  package=com.amazon.firetv.youtube
  active=true
  state=PlaybackState {state=3, position=60000, buffered position=90000, speed=1.0, updated=3990000, actions=895, custom actions=[], active item id=-1, error=null}
  metadata:size=4, description=Dune: Part Two trailer, Warner Bros., null

Global priority session is null
Sessions Stack - have 2 sessions:
  YouTube com.amazon.firetv.youtube/YouTube
"""


def test_playing_reads_the_session_of_the_app_in_front_and_its_position_now():
    # Uptime 4000 s; the position was 60 s at 3990 s, playing at speed 1.
    answer = playing(MEDIA_DUMP + "4000.00 3000.00\n", "com.amazon.firetv.youtube/dev.cobalt.app.MainActivity")
    assert answer == {"app": "com.amazon.firetv.youtube", "state": "playing", "position_seconds": 70, "title": "Dune: Part Two trailer"}
    # Another app in front: the one that plays is still found.
    assert playing(MEDIA_DUMP + "4000.00 3000.00\n", NETFLIX)["app"] == "com.amazon.firetv.youtube"
    assert playing(MEDIA_DUMP.split("YouTube com.amazon")[0], NETFLIX) is None


async def test_media_presses_the_keys_and_reports_what_plays():
    tv = FakeTV()
    tv.front = "com.amazon.firetv.youtube/dev.cobalt.app.MainActivity"
    tv.media_dump = MEDIA_DUMP
    seen = await FakeAdbComputer(tv).media("forward", 3)
    assert tv.steps[-1][0] == "key:90,90,90" and "dumpsys media_session" in tv.commands[-1]
    assert seen.media["state"] == "playing"
    await FakeAdbComputer(tv).media("pause", 3)
    assert tv.steps[-1][0] == "key:127"
    tv.media_dump = ""
    seen = await FakeAdbComputer(tv).media("state", 1)
    assert tv.steps[-1] == ["look:1280x720,auto"] and seen.media["state"].startswith("unknown")


async def test_a_connection_the_power_cut_is_made_again_but_a_timeout_isnt_run_twice():
    tv = FakeTV()
    computer = FakeAdbComputer(tv)
    await computer.keys(["Up"])
    tv.dead = True
    with pytest.raises(ComputerError, match="stopped answering"):
        await computer.keys(["Up"])
    # Back on: the next call connects again.
    tv.dead = False
    await computer.keys(["Up"])
    assert tv.connects == 3 and tv.steps[-1][0] == "key:19"
    # A call that timed out may have pressed its keys: not again.
    tv.hangs = True
    count = len(tv.commands)
    with pytest.raises(ComputerError, match="stopped answering"):
        await computer.keys(["Up"])
    assert len(tv.commands) == count
    tv.hangs = False
    tv.off = True
    computer._device = None
    with pytest.raises(ComputerError, match="doesn't answer"):
        await computer.keys(["Up"])


async def test_a_helper_that_printed_nothing_is_an_error():
    tv = FakeTV()
    computer = FakeAdbComputer(tv)

    def broken(command: str) -> bytes:
        return b"HUVU-STATE\nError: could not find class\nHUVU-STATE\n"

    tv.run = broken
    with pytest.raises(ComputerError, match="helper failed: Error: could not find class"):
        await computer.look("auto")


async def test_huvu_opens_once_after_the_box_boots(monkeypatch):
    monkeypatch.setattr(external_computer, "BOOT_SETTLE", 0)
    tv = FakeTV()
    computer = FakeAdbComputer(tv, open_on_boot="com.huvu.tv/.MainActivity")
    await computer._check()
    assert tv.front == "com.huvu.tv/.MainActivity"
    assert sum("am start -n com.huvu.tv/.MainActivity" in c for c in tv.commands) == 1
    # The same boot, again: nothing more.
    tv.front = NETFLIX
    await computer._check()
    assert tv.front == NETFLIX


async def test_a_box_that_was_on_long_before_isnt_taken_over(monkeypatch):
    monkeypatch.setattr(external_computer, "BOOT_SETTLE", 0)
    tv = FakeTV()
    tv.uptime = 4000.0
    await FakeAdbComputer(tv, open_on_boot="com.huvu.tv/.MainActivity")._check()
    assert tv.front == "com.amazon.tv.launcher/.ui.HomeActivity_vNext"


def test_the_helper_is_built():
    """The jar the backend pushes is in the package, and is a dex jar."""
    jar = external_computer.HELPER
    assert jar.exists() and jar.read_bytes()[:2] == b"PK"


# The route and the plugin


@pytest.fixture
def served(tmp_path):
    computer = FakeComputer()
    kernel = Kernel(Config(data_dir=tmp_path, hermes_home=tmp_path / "hermes", public_url="http://test"), hermes=FakeHermes(), computer=computer)
    token = kernel.accounts.add_user("me", "Me", "correct horse", "key-me")
    app = create_app(kernel.config, kernel)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", kernel, computer, token
    server.should_exit = True
    thread.join(5)


def test_the_route_runs_an_action_for_a_session_that_may_browse(served, monkeypatch):
    base, kernel, computer, token = served
    headers = {"Authorization": f"Bearer {token}"}
    body = {"session": "s1", "action": "key", "args": {"keys": ["Home"]}}
    assert httpx.post(f"{base}/api/computer", json=body).status_code == 401
    monkeypatch.setattr(kernel, "may_browse", lambda session, user, group: False)
    assert httpx.post(f"{base}/api/computer", json=body, headers=headers).json() == {"error": COMPUTER_BLOCKED}
    assert computer.did == []
    monkeypatch.setattr(kernel, "may_browse", lambda session, user, group: (session, user) == ("s1", "me"))
    answer = httpx.post(f"{base}/api/computer", json=body, headers=headers).json()
    assert computer.did == [("keys", "Home")]
    assert answer["result"]["in_front"] == NETFLIX and answer["result"]["elements"] == "[1] Play (highlighted)"
    assert base64.b64decode(answer["image"]) == JPEG
    bad = httpx.post(f"{base}/api/computer", json={"session": "s1", "action": "fly"}, headers=headers).json()
    assert "no action 'fly'" in bad["error"]


def test_the_plugin_hands_the_model_the_screenshot(monkeypatch):
    plugin = load_plugin()
    sent: list[tuple[str, dict]] = []
    image = base64.b64encode(JPEG).decode()

    def post(path, body, timeout=3):
        sent.append((path, body))
        return {"result": {"action": "key", "screen": "on"}, "image": image}

    monkeypatch.setattr(plugin, "_post", post)
    out = plugin._external_computer({"action": "key", "keys": ["Home"]}, session_id="s1")
    assert sent == [("/api/computer", {"session": "s1", "action": "key", "args": {"keys": ["Home"]}})]
    assert out["_multimodal"] is True
    assert json.loads(out["content"][0]["text"]) == {"action": "key", "screen": "on"}
    assert out["content"][1]["image_url"]["url"] == f"data:image/jpeg;base64,{image}"
    # No screenshot, plain text; a refusal, the reason.
    monkeypatch.setattr(plugin, "_post", lambda path, body, timeout=3: {"result": {"elements": "[1] Find"}, "image": None})
    assert json.loads(plugin._external_computer({"action": "look"}, session_id="s1")) == {"elements": "[1] Find"}
    monkeypatch.setattr(plugin, "_post", lambda path, body, timeout=3: {"error": COMPUTER_BLOCKED})
    assert json.loads(plugin._external_computer({"action": "screenshot"}, session_id="s1")) == {"error": COMPUTER_BLOCKED}


def test_the_plugin_registers_the_tool():
    plugin = load_plugin()

    class Ctx:
        def __init__(self) -> None:
            self.tools: dict[str, dict] = {}

        def register_hook(self, name, fn) -> None:
            pass

        def register_tool(self, name, toolset, schema, handler, description=""):
            self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}

    ctx = Ctx()
    plugin.register(ctx)
    tool = ctx.tools["external_computer"]
    assert tool["toolset"] == "external_computer" and tool["schema"]["name"] == "external_computer"
    assert set(tool["schema"]["parameters"]["properties"]["action"]["enum"]) == set(external_computer.ACTIONS)
    assert set(tool["schema"]["parameters"]["properties"]["command"]["enum"]) == set(external_computer.MEDIA)
