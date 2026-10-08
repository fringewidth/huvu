"""huvu's voice: Microsoft's Edge read-aloud voices (edge-tts), or Kokoro
(kokoro-onnx) run on this machine.

A turn's answer is spoken while huvu writes it. `speak` follows the turn's
events, cuts the answer into sentences as each one is finished, and turns
each into audio in a worker thread while the client plays the one before.
So huvu starts talking after its first sentence, not after its last. The
audio is raw PCM: 16-bit little-endian, mono, SAMPLE_RATE Hz, with no
header, so the client can play each piece as it arrives.

Edge is the default (decided 2026-10-08): free, no account, the same
neural voices as Azure, Indian English among them, and no load on the
backend's CPU. Hermes uses it too. It is an unofficial endpoint Microsoft
could close; each sentence comes back as MP3 at 24 kHz, decoded here with
miniaudio. A sentence Edge fails on is said by Kokoro, when its model is
installed.

Kokoro keeps up only on a machine that makes speech faster than it plays:
about 3.5 times faster than real time on an Apple M4 Pro (measured
2026-10-08). Its files live in <data_dir>/models (`kokoro-v1.0.onnx` and
`voices-v1.0.bin`, from github.com/thewh1teagle/kokoro-onnx releases).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)

SAMPLE_RATE = 24000
# A sentence longer than this with no end in sight is cut at a comma or a
# space, so a long run-on doesn't keep huvu quiet.
LONGEST = 240

# The end of a sentence that has something after it already, or a line
# break. A sentence at the very end of the text so far may still grow
# ("3." becoming "3.5"), so it waits for what comes next.
_END = re.compile(r"[.!?…]+[\"')\]”’*_]*(?=\s)|\n+")


def sentences(text: str, start: int, final: bool) -> tuple[list[str], int]:
    """The finished sentences in text[start:], and where the unfinished
    rest begins. With `final`, the rest is a sentence too."""
    found: list[str] = []
    for match in _END.finditer(text, start):
        found.append(text[start : match.end()])
        start = match.end()
    if final:
        found.append(text[start:])
        start = len(text)
    elif len(text) - start > LONGEST:
        window = text[start : start + LONGEST]
        cut = max(window.rfind(", "), window.rfind("; "), window.rfind(" "))
        if cut > 0:
            found.append(window[: cut + 1])
            start += cut + 1
    return [s for s in (speakable(f) for f in found) if s], start


def speakable(text: str) -> str:
    """The words of a piece of markdown, as they should be read aloud."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"^\s*(?:[-*•]|\d+\.)\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_`#>]+", "", text)
    return " ".join(text.split())


# Each engine's voice when the config names none.
DEFAULT_VOICES = {"edge": "en-IN-NeerjaExpressiveNeural", "kokoro": "af_heart"}


class Voice:
    def __init__(self, config: Config) -> None:
        self._engine_name = config.speech_engine
        self._edge_voice = config.speech_voice if config.speech_engine == "edge" and config.speech_voice else DEFAULT_VOICES["edge"]
        self._model = config.data_dir / "models" / "kokoro-v1.0.onnx"
        self._voices = config.data_dir / "models" / "voices-v1.0.bin"
        self._voice = config.speech_voice if config.speech_engine == "kokoro" and config.speech_voice else DEFAULT_VOICES["kokoro"]
        self._lang = config.speech_lang
        self._speed = config.speech_speed
        self._engine: Any = None
        # One worker: Kokoro is CPU bound, and two at once would only slow
        # each other down. MP3 decoding runs here too.
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice")

    def _kokoro_installed(self) -> bool:
        return self._model.exists() and self._voices.exists()

    def available(self) -> bool:
        return self._engine_name == "edge" or self._kokoro_installed()

    async def warm(self) -> None:
        """Loads Kokoro and makes one word, so the first real sentence
        doesn't pay for the start. Edge needs no warming."""
        if self._kokoro_installed():
            await self._kokoro("Hi.")

    async def pcm(self, text: str) -> bytes:
        if self._engine_name == "edge":
            try:
                return await self._edge(text)
            except Exception:
                if not self._kokoro_installed():
                    raise
                log.warning("voice: Edge failed, Kokoro says %r", text, exc_info=True)
        return await self._kokoro(text)

    async def _edge(self, text: str) -> bytes:
        import edge_tts

        rate = f"{round((self._speed - 1) * 100):+d}%"
        mp3 = b""
        async for chunk in edge_tts.Communicate(text, self._edge_voice, rate=rate).stream():
            if chunk["type"] == "audio":
                mp3 += chunk["data"]
        if not mp3:
            raise RuntimeError(f"Edge said nothing for {text!r}")
        return await asyncio.get_running_loop().run_in_executor(self._worker, _decode_mp3, mp3)

    async def _kokoro(self, text: str) -> bytes:
        return await asyncio.get_running_loop().run_in_executor(self._worker, self._make, text)

    def _make(self, text: str) -> bytes:
        if self._engine is None:
            from kokoro_onnx import Kokoro

            self._engine = Kokoro(str(self._model), str(self._voices))
        samples, rate = self._engine.create(text, voice=self._voice, speed=self._speed, lang=self._lang)
        if rate != SAMPLE_RATE:
            raise RuntimeError(f"Kokoro made {rate} Hz audio, not {SAMPLE_RATE}")
        return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()

    async def speak(self, events: AsyncIterator[tuple[str, dict[str, Any]]]) -> AsyncIterator[bytes]:
        """The audio of a turn's answer, a sentence at a time, from the
        turn's events (ConversationHub.replay)."""
        text = ""
        spoken = 0
        async for name, data in events:
            if name == "answer.delta":
                text += data.get("text", "")
                final = False
            elif name == "turn.ended":
                # The whole answer, the same text the deltas built.
                text = data.get("answer") or text
                final = True
            else:
                continue
            ready, spoken = sentences(text, spoken, final)
            for sentence in ready:
                try:
                    yield await self.pcm(sentence)
                except Exception:
                    log.exception("voice: couldn't say %r", sentence)
            if final:
                return

    def close(self) -> None:
        self._worker.shutdown(wait=False, cancel_futures=True)


def _decode_mp3(mp3: bytes) -> bytes:
    import miniaudio

    decoded = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=SAMPLE_RATE)
    return decoded.samples.tobytes()
