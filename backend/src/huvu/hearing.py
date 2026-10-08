"""huvu's hearing: speech to text through Groq's Whisper (free tier).

The app records an ask and sends the clip here (`POST /api/transcribe`);
it goes to Groq's OpenAI-compatible transcription endpoint and comes back
as text. Whisper is told the language is English and is given a prompt
with the family's names and huvu's, so they come out spelled right.

GROQ_API_KEY comes from the environment or backend/.env. Without it there
is no hearing, and the app falls back to the browser's own recognition.

Groq limits each model apart (on the free tier, 20 requests a minute and
2,000 a day each), so a clip the configured model is rate limited on goes
to FALLBACK_MODEL next. When both are limited, the app hears 429 and falls
back to its own recognition.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from .prompts import whisper_prompt

if TYPE_CHECKING:
    from .kernel import Kernel

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
FALLBACK_MODEL = "whisper-large-v3-turbo"
# Groq refuses files over 25 MB on the free tier; an ask is far smaller.
LARGEST = 25 * 1024 * 1024
# Container types the app's MediaRecorder makes, and the file name Groq
# needs to tell them apart.
SUFFIXES = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav"}


class HearingError(Exception):
    def __init__(self, message: str, *, limited: bool = False) -> None:
        super().__init__(message)
        # Groq said too many requests, on every model tried.
        self.limited = limited


class Hearing:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self._key = kernel.config.groq_api_key
        self._model = kernel.config.stt_model
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    def available(self) -> bool:
        return bool(self._key)

    def prompt(self) -> str:
        """Names Whisper should expect: huvu's and the family's."""
        users = self._kernel.accounts.users()
        return whisper_prompt([u.name for u in users] + [u.id for u in users])

    async def transcribe(self, audio: bytes, content_type: str) -> str:
        if not self._key:
            raise HearingError("no GROQ_API_KEY")
        if not audio:
            return ""
        if len(audio) > LARGEST:
            raise HearingError("the clip is over 25 MB")
        kind = content_type.split(";")[0].strip().lower()
        name = f"ask.{SUFFIXES.get(kind, 'webm')}"
        models = list(dict.fromkeys([self._model, FALLBACK_MODEL]))
        for model in models:
            res = await self._http.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {self._key}"},
                files={"file": (name, audio, kind or "application/octet-stream")},
                data={"model": model, "language": "en", "prompt": self.prompt(), "response_format": "json", "temperature": "0"},
            )
            if res.status_code == 429:
                continue
            if res.status_code != 200:
                raise HearingError(f"Groq answered {res.status_code}: {res.text[:200]}")
            return res.json().get("text", "").strip()
        raise HearingError(f"Groq's rate limit on {', '.join(models)}", limited=True)

    async def close(self) -> None:
        await self._http.aclose()
