"""The external computer: the tool's actions, the Fire TV over a fake ADB
device, the backend's route and the plugin's tool."""

from __future__ import annotations

import base64
import json
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import uvicorn

from huvu import external_computer
from huvu.config import Config
from huvu.external_computer import AdbComputer, ComputerError, ExternalComputer, Look, use
from huvu.http import create_app
from huvu.kernel import Kernel
from huvu.prompts import COMPUTER_BLOCKED

from .fakes import FakeHermes
from .test_hermes_plugin import load as load_plugin

NOW = datetime(2026, 10, 17, 13, 0)
JPEG = b"\xff\xd8 a screenshot"


class FakeComputer(ExternalComputer):
    def __init__(self, screen: str = "on") -> None:
        self.screen = screen
        self.did: list[tuple] = []

    async def look(self) -> Look:
        jpeg = None if self.screen == "off" else JPEG
        return Look("com.netflix.ninja/.MainActivity", self.screen, jpeg, (1280, 720))

    async def click(self, x: int, y: int) -> None:
        self.did.append(("click", x, y))

    async def keys(self, names: list[str]) -> None:
        self.did.append(("keys", *names))

    async def type(self, text: str) -> None:
        self.did.append(("type", text))

    async def open(self, target: str) -> None:
        self.did.append(("open", target))

    async def apps(self) -> list[str]:
        return ["com.huvu.tv", "com.netflix.ninja"]


@pytest.fixture(autouse=True)
def no_settling(monkeypatch):
    monkeypatch.setattr(external_computer, "SETTLE", 0)
    monkeypatch.setattr(external_computer, "SETTLE_OPEN", 0)


async def test_an_action_answers_with_the_screen_after_it(tmp_path):
    computer = FakeComputer()
    outcome = await use(computer, "key", {"keys": ["Down", "Down", "Return"]}, tmp_path, NOW)
    assert computer.did == [("keys", "Down", "Down", "Return")]
    assert outcome.result["in_front"] == "com.netflix.ninja/.MainActivity" and outcome.result["screen"] == "on"
    assert outcome.result["screenshot_size"] == [1280, 720]
    # The screenshot is kept, for the model to show its person.
    assert Path(outcome.result["screenshot"]).read_bytes() == JPEG
    assert base64.b64decode(outcome.image) == JPEG


async def test_apps_and_a_screen_that_is_off_have_no_screenshot(tmp_path):
    apps = await use(FakeComputer(), "apps", {}, tmp_path, NOW)
    assert apps.result == {"action": "apps", "apps": ["com.huvu.tv", "com.netflix.ninja"]} and apps.image is None
    off = await use(FakeComputer(screen="off"), "screenshot", {}, tmp_path, NOW)
    assert off.result["screen"] == "off" and "screenshot" not in off.result and off.image is None


async def test_actions_check_their_arguments(tmp_path):
    computer = FakeComputer()
    for action, args in [("click", {"x": 10}), ("key", {}), ("type", {"text": ""}), ("open", {}), ("wait", {"seconds": "soon"}), ("scroll", {})]:
        with pytest.raises(ComputerError):
            await use(computer, action, args, tmp_path, NOW)
    assert computer.did == []
    # Keys may come as one string too.
    await use(computer, "key", {"keys": "Up Return"}, tmp_path, NOW)
    assert computer.did == [("keys", "Up", "Return")]


class BlindComputer(FakeComputer):
    async def look(self) -> Look:
        raise ComputerError("the TV box stopped answering")


async def test_an_action_that_ran_says_so_when_the_screen_cant_be_seen(tmp_path):
    computer = BlindComputer()
    outcome = await use(computer, "open", {"target": "com.netflix.ninja"}, tmp_path, NOW)
    assert computer.did == [("open", "com.netflix.ninja")]
    assert outcome.result == {"action": "open", "target": "com.netflix.ninja", "screen": "unseen: the TV box stopped answering"}
    assert outcome.image is None
    # Seeing the screen is all a screenshot does, so it fails.
    with pytest.raises(ComputerError, match="stopped answering"):
        await use(computer, "screenshot", {}, tmp_path, NOW)


async def test_only_the_latest_screenshots_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(external_computer, "KEEP_SHOTS", 2)
    for second in range(4):
        await use(FakeComputer(), "screenshot", {}, tmp_path, NOW.replace(second=second))
    assert len(list(tmp_path.glob("*.jpg"))) == 2


# The Fire TV over ADB


class FakeDevice:
    """Answers the shell commands AdbComputer sends, and records them."""

    def __init__(self, tv: "FakeTV") -> None:
        self.tv = tv

    async def shell(self, command: str, read_timeout_s: float = 10.0) -> str:
        if self.tv.dead:
            raise ConnectionResetError("the TV lost power")
        self.tv.commands.append(command)
        return self.tv.answer(command)

    async def exec_out(self, command: str, read_timeout_s: float = 10.0, decode: bool = True) -> bytes:
        if self.tv.dead:
            raise ConnectionResetError("the TV lost power")
        self.tv.commands.append(command)
        if command.startswith("screenrecord") and self.tv.record_hangs:
            raise TimeoutError("screenrecord never ended")
        return b"a video" if command.startswith("screenrecord") else b"a png"

    async def close(self) -> None:
        self.tv.closed += 1


class FakeTV:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.connects = 0
        self.closed = 0
        self.dead = False
        self.off = False
        self.record_hangs = False
        self.front = "com.amazon.tv.launcher/.ui.HomeActivity"
        self.uptime = 40.0
        self.boot = "boot-1"
        self.installed = {"com.netflix.ninja": "LEANBACK_LAUNCHER", "com.ghisler.android.TotalCommander": "LAUNCHER"}

    def answer(self, command: str) -> str:
        if command == "wm size":
            return "Physical size: 1920x1080\r\n"
        if command.startswith("dumpsys window"):
            return f"  mCurrentFocus=Window{{24e92aae u0 {self.front}}}\r\n" if self.front else "  mCurrentFocus=null\r\n"
        if command.startswith("dumpsys power"):
            return "  mWakefulness=Awake\r\n"
        if command.startswith("cat /proc/sys/kernel/random/boot_id"):
            return f"{self.boot}\r\n{self.uptime} 12.0\r\n"
        if command.startswith("monkey"):
            package, category = command.split()[2], command.split()[4].rsplit(".", 1)[1]
            return "Events injected: 1\r\n" if self.installed.get(package) == category else "** No activities found to run, monkey aborted.\r\n"
        if command.startswith("am start -n com.huvu.tv") or "am start -n com.huvu.tv" in command:
            self.front = "com.huvu.tv/.MainActivity"
            return "Starting: Intent { cmp=com.huvu.tv/.MainActivity }\r\n"
        if command.startswith("am start -n"):
            return "Error: Activity class does not exist.\r\n"
        if command.startswith("pm list packages"):
            return "package:com.netflix.ninja\r\npackage:com.huvu.tv\r\n"
        return ""


class FakeAdbComputer(AdbComputer):
    def __init__(self, tv: FakeTV, open_on_boot: str | None = None) -> None:
        super().__init__("192.168.29.7", 5555, Path("adbkey"), open_on_boot)
        self.tv = tv

    async def _connect(self) -> FakeDevice:
        if self.tv.off:
            raise ComputerError("the TV box doesn't answer")
        self.tv.connects += 1
        self._size = None
        return FakeDevice(self.tv)


@pytest.fixture
def frames(monkeypatch):
    """ffmpeg's part: the last frame of what the TV sent."""
    seen: list[tuple[bytes, int, int]] = []

    async def last_frame(media: bytes, width: int, height: int) -> bytes:
        seen.append((media, width, height))
        return JPEG

    monkeypatch.setattr(external_computer, "last_frame", last_frame)
    return seen


async def test_keys_go_in_one_call_as_android_key_codes():
    tv = FakeTV()
    computer = FakeAdbComputer(tv)
    await computer.keys(["Down", "arrow-down", "Return", "Escape", "Play_Pause"])
    assert tv.commands == ["input keyevent 20 20 23 4 85"]
    with pytest.raises(ComputerError, match="no key 'Ctrl'"):
        await computer.keys(["Down", "Ctrl"])
    assert len(tv.commands) == 1


async def test_clicks_are_scaled_from_the_screenshot_to_the_screen():
    tv = FakeTV()
    computer = FakeAdbComputer(tv)
    await computer.click(640, 360)
    assert tv.commands[-1] == "input tap 960 540"
    with pytest.raises(ComputerError, match="off the screen"):
        await computer.click(1280, 10)


async def test_typing_turns_spaces_into_what_input_reads():
    tv = FakeTV()
    computer = FakeAdbComputer(tv)
    await computer.type("the dark knight's")
    assert tv.commands == ["input text 'the%sdark%sknight'\"'\"'s'"]
    with pytest.raises(ComputerError):
        await computer.type("café")


async def test_open_takes_a_package_a_component_or_a_link():
    tv = FakeTV()
    computer = FakeAdbComputer(tv)
    await computer.open("com.netflix.ninja")
    assert tv.commands[-1] == "monkey -p com.netflix.ninja -c android.intent.category.LEANBACK_LAUNCHER 1"
    # An app with only a phone's launcher icon.
    await computer.open("com.ghisler.android.TotalCommander")
    assert tv.commands[-1].endswith("android.intent.category.LAUNCHER 1")
    await computer.open("https://www.youtube.com/watch?v=abc")
    assert tv.commands[-1] == "am start -a android.intent.action.VIEW -d 'https://www.youtube.com/watch?v=abc'"
    with pytest.raises(ComputerError, match="didn't open"):
        await computer.open("com.example.missing")
    with pytest.raises(ComputerError, match="didn't open"):
        await computer.open("com.example/.Missing")
    with pytest.raises(ComputerError, match="package name"):
        await computer.open("netflix; reboot")


async def test_a_look_is_a_frame_of_a_short_recording(frames):
    tv = FakeTV()
    seen = await FakeAdbComputer(tv).look()
    assert seen == Look("com.amazon.tv.launcher/.ui.HomeActivity", "on", JPEG, (1280, 720))
    assert any(c.startswith("screenrecord --size 1280x720") for c in tv.commands)
    assert frames == [(b"a video", 1280, 720)]


async def test_a_recording_that_hangs_gives_way_to_a_screencap(frames):
    tv = FakeTV()
    tv.record_hangs = True
    seen = await FakeAdbComputer(tv).look()
    assert seen.jpeg == JPEG
    # The recording is tried once, on the connection it hung on.
    assert sum(c.startswith("screenrecord") for c in tv.commands) == 1
    assert tv.commands[-1] == "screencap -p" and frames[-1] == (b"a png", 1280, 720)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
async def test_ffmpeg_takes_the_last_frame_as_a_jpeg(tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=5:duration=1", "-pix_fmt", "yuv420p", str(clip)], check=True)
    jpeg = await external_computer.last_frame(clip.read_bytes(), 160, 90)
    assert jpeg.startswith(b"\xff\xd8")
    with pytest.raises(ComputerError, match="couldn't be read"):
        await external_computer.last_frame(b"not a video", 160, 90)


async def test_a_connection_the_power_cut_is_made_again():
    tv = FakeTV()
    computer = FakeAdbComputer(tv)
    await computer.keys(["Up"])
    tv.dead = True
    with pytest.raises(ComputerError, match="stopped answering"):
        await computer.keys(["Up"])
    # Back on: the next call connects again.
    tv.dead = False
    await computer.keys(["Up"])
    assert tv.connects == 3 and tv.commands[-1] == "input keyevent 19"
    tv.off = True
    computer._device = None
    with pytest.raises(ComputerError, match="doesn't answer"):
        await computer.keys(["Up"])


async def test_huvu_opens_once_after_the_box_boots(monkeypatch):
    monkeypatch.setattr(external_computer, "BOOT_SETTLE", 0)
    tv = FakeTV()
    computer = FakeAdbComputer(tv, open_on_boot="com.huvu.tv/.MainActivity")
    await computer._check()
    assert tv.front == "com.huvu.tv/.MainActivity"
    assert sum("am start -n com.huvu.tv/.MainActivity" in c for c in tv.commands) == 1
    # The same boot, again: nothing more.
    tv.front = "com.netflix.ninja/.MainActivity"
    await computer._check()
    assert tv.front == "com.netflix.ninja/.MainActivity"


async def test_a_box_that_was_on_long_before_isnt_taken_over(monkeypatch):
    monkeypatch.setattr(external_computer, "BOOT_SETTLE", 0)
    tv = FakeTV()
    tv.uptime = 4000.0
    await FakeAdbComputer(tv, open_on_boot="com.huvu.tv/.MainActivity")._check()
    assert tv.front == "com.amazon.tv.launcher/.ui.HomeActivity"


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
    assert answer["result"]["in_front"] == "com.netflix.ninja/.MainActivity"
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
    monkeypatch.setattr(plugin, "_post", lambda path, body, timeout=3: {"result": {"apps": []}, "image": None})
    assert json.loads(plugin._external_computer({"action": "apps"}, session_id="s1")) == {"apps": []}
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
