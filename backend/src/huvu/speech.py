"""huvu's voice: Microsoft's Edge read-aloud voices, through edge-tts.

A turn's answer is spoken while huvu writes it. `speak` follows the turn's
events, cuts the answer into sentences as each one is finished, and has
each said while the client plays the one before. So huvu starts talking
after its first sentence, not after its last. Each sentence is one MP3 clip,
as Edge made it (24 kHz mono, 48 kbps), sent as a frame: its length as four
bytes, big-endian, then the clip. The client decodes each clip itself.

Edge was picked on 2026-10-08: free, no account, the same neural voices as
Azure, and no load on the backend's CPU. Hermes uses it too. It is an
unofficial endpoint Microsoft could close, and while it is down huvu is
silent; its answers still show. Kokoro, run locally, was tried first and
removed: it sounded too cheap. Decoding the MP3 here (miniaudio) was dropped
too: it had no build for Python 3.14, and MP3 is an eighth the size of PCM
over a phone's data.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)

MEDIA_TYPE = "application/vnd.huvu.speech"
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


# Emoji, their joiners and variation selectors: the voice would read their
# names, or stumble.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")


def speakable(text: str) -> str:
    """The words of a piece of markdown, as they should be read aloud."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"^\s*(?:[-*•]|\d+\.)\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_`#>]+", "", text)
    text = _EMOJI.sub("", text)
    return " ".join(text.split())


class Voice:
    def __init__(self, config: Config) -> None:
        self._voice = config.speech_voice
        self._rate = f"{round((config.speech_speed - 1) * 100):+d}%"

    async def clip(self, text: str) -> bytes:
        """One sentence as an MP3 clip."""
        import edge_tts

        mp3 = b""
        async for chunk in edge_tts.Communicate(text, self._voice, rate=self._rate).stream():
            if chunk["type"] == "audio":
                mp3 += chunk["data"]
        if not mp3:
            raise RuntimeError(f"Edge said nothing for {text!r}")
        return mp3

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
                    clip = await self.clip(sentence)
                    yield len(clip).to_bytes(4, "big") + clip
                except Exception:
                    log.exception("voice: couldn't say %r", sentence)
            if final:
                return

