"""Each person's recordings for training the "hey huvu" model (docs/wake-word.md).

The app's recording screen walks a person through saying "hey huvu" near the
phone, far from it, in another room and over noise, then phrases that sound
close to it and a little ordinary talk. It sends each clip it keeps here. The
training on the dev machine copies the folder from the host.

A clip is 16 kHz mono 16-bit WAV, what the wake word listens to on the phone.
It is kept at `<data dir>/voice_clips/<user>/<kind>/<step>/<id>.wav`: `wake`
clips say the phrase, `other` clips don't. The id starts with the kind of
device it was recorded on, since a laptop's mic sounds unlike a phone's.
"""

from __future__ import annotations

import io
import re
import secrets
import wave
from datetime import datetime
from pathlib import Path

FOLDER = "voice_clips"
KINDS = ("wake", "other")
RATE = 16000
MIN_SECONDS = 0.2
MAX_SECONDS = 30
STEP = re.compile(r"^[a-z_]{1,32}$")
CLIP_ID = re.compile(r"^[a-z]+-\d{8}T\d{6}-[0-9a-f]{6}$")


class ClipError(Exception):
    """A clip refused, with the reason for the person."""


def save(data_dir: Path, user: str, kind: str, step: str, device_kind: str, audio: bytes, now: datetime) -> str:
    """Keeps one clip, and returns its id."""
    if kind not in KINDS:
        raise ClipError(f"a clip is {' or '.join(KINDS)}, not {kind!r}")
    if not STEP.match(step):
        raise ClipError(f"no step {step!r}")
    _check(audio)
    clip_id = f"{device_kind}-{now:%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"
    folder = data_dir / FOLDER / user / kind / step
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{clip_id}.wav").write_bytes(audio)
    return clip_id


def counts(data_dir: Path, user: str) -> dict[str, int]:
    """How many clips the person has kept, by step."""
    found: dict[str, int] = {}
    for clip in (data_dir / FOLDER / user).glob("*/*/*.wav"):
        step = clip.parent.name
        found[step] = found.get(step, 0) + 1
    return found


def delete(data_dir: Path, user: str, clip_id: str) -> None:
    """Drops one of the person's clips: the screen's "that wasn't it"."""
    if not CLIP_ID.match(clip_id):
        raise ClipError(f"no clip {clip_id!r}")
    found = list((data_dir / FOLDER / user).glob(f"*/*/{clip_id}.wav"))
    if not found:
        raise ClipError(f"no clip {clip_id!r}")
    for clip in found:
        clip.unlink()


def _check(audio: bytes) -> None:
    try:
        with wave.open(io.BytesIO(audio)) as clip:
            shape = (clip.getnchannels(), clip.getsampwidth(), clip.getframerate(), clip.getcomptype())
            seconds = clip.getnframes() / clip.getframerate()
    except (wave.Error, EOFError) as err:
        raise ClipError(f"not a WAV file: {err}") from err
    if shape != (1, 2, RATE, "NONE"):
        raise ClipError(f"a clip is 16 kHz mono 16-bit PCM; this one is {shape[2]} Hz, {shape[0]} channels, {8 * shape[1]}-bit")
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise ClipError(f"a clip is {MIN_SECONDS} to {MAX_SECONDS} s long; this one is {seconds:.1f} s")
