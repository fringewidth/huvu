"""Asks through Alexa, for the Fire TV, which gives apps no mic.

A custom Alexa skill, kept in development stage so it needs no review
("Alexa, ask huvu ..."), posts each ask to `POST /api/alexa`. Its one
intent holds the whole sentence in a catch-all slot
(docs/alexa-interaction-model.json). huvu checks that the request is
Amazon's and for its skill, asks it in the conversation of the household TV
that shows huvu, and answers Alexa at once with silence: the TV shows the
turn and says the answer.

The checks are Amazon's, for a skill hosted as a web service: the
certificate chain's URL, the chain (in date, for echo-api.amazon.com, up to
a trusted root), the signature over the request's exact bytes, and a
timestamp within 150 seconds. Then the skill's id.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import posixpath
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import certifi
import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

if TYPE_CHECKING:
    from .accounts import Device
    from .kernel import Kernel

log = logging.getLogger("huvu.alexa")

SIGNER = "echo-api.amazon.com"
MAX_AGE = timedelta(seconds=150)
ASK_INTENT = "AskIntent"
ASK_SLOT = "ask"

# What Alexa says. Everything else it leaves to the TV.
NO_TV = "huvu isn't open on the TV."
NOT_HEARD = "I didn't catch that."
HELP = "Say, Alexa, ask huvu, and then your question. The answer shows on the TV."


class AlexaError(Exception):
    """The request isn't Amazon's, or isn't for this skill."""


def chain_url_ok(url: str) -> bool:
    """Whether Amazon's certificate chain is at `url`: https, on
    s3.amazonaws.com, port 443 if any, under /echo.api/."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return False
    path = posixpath.normpath(parts.path) if parts.path else ""
    return parts.scheme.lower() == "https" and (parts.hostname or "").lower() == "s3.amazonaws.com" and port in (None, 443) and path.startswith("/echo.api/")


@dataclass
class _Chain:
    leaf: x509.Certificate
    intermediates: list[x509.Certificate]


class Verifier:
    """Checks a request's signature against Amazon's certificate chain.
    Chains are fetched once per URL and kept while their certificate is in
    date."""

    def __init__(self, fetch: Callable[[str], Awaitable[bytes]] | None = None, roots: list[x509.Certificate] | None = None) -> None:
        self._fetch = fetch or _fetch
        self._store = Store(roots or x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes()))
        self._chains: dict[str, _Chain] = {}

    async def verify(self, chain_url: str, signature: str, body: bytes, now: datetime) -> None:
        if not chain_url_ok(chain_url):
            raise AlexaError(f"certificate chain URL {chain_url!r} isn't Amazon's")
        chain = await self._chain(chain_url, now)
        key = chain.leaf.public_key()
        if not isinstance(key, rsa.RSAPublicKey):
            raise AlexaError("the signing certificate's key isn't RSA")
        try:
            key.verify(base64.b64decode(signature), body, padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError) as err:
            raise AlexaError("the signature doesn't match the request") from err

    async def _chain(self, url: str, now: datetime) -> _Chain:
        kept = self._chains.get(url)
        if kept and kept.leaf.not_valid_after_utc > now:
            return kept
        try:
            certs = x509.load_pem_x509_certificates(await self._fetch(url))
        except (httpx.HTTPError, ValueError) as err:
            raise AlexaError(f"couldn't get the certificate chain: {err}") from err
        chain = _Chain(certs[0], certs[1:])
        verifier = PolicyBuilder().store(self._store).time(now).build_server_verifier(x509.DNSName(SIGNER))
        try:
            verifier.verify(chain.leaf, chain.intermediates)
        except VerificationError as err:
            raise AlexaError(f"the certificate chain isn't valid for {SIGNER}: {err}") from err
        self._chains[url] = chain
        return chain


async def _fetch(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=5) as http:
        res = await http.get(url)
        res.raise_for_status()
        return res.content


def reply(speech: str | None = None) -> dict[str, Any]:
    """Alexa's answer: silence, or one short line, and the session ends."""
    response: dict[str, Any] = {"shouldEndSession": True}
    if speech:
        response["outputSpeech"] = {"type": "PlainText", "text": speech}
    return {"version": "1.0", "response": response}


class Alexa:
    def __init__(self, kernel: Kernel, skill_id: str, verifier: Verifier | None = None) -> None:
        self._kernel = kernel
        self._skill_id = skill_id
        self._verifier = verifier or Verifier()
        self._asks: set[asyncio.Task[None]] = set()

    async def handle(self, headers: Mapping[str, str], body: bytes) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        await self._verifier.verify(headers.get("SignatureCertChainUrl", ""), headers.get("Signature-256", ""), body, now)
        envelope = _json(body)
        request = envelope.get("request", {})
        try:
            at = datetime.fromisoformat(request.get("timestamp", ""))
        except ValueError as err:
            raise AlexaError("the request has no timestamp") from err
        if abs(now - at) > MAX_AGE:
            raise AlexaError("the request is too old")
        skill = envelope.get("context", {}).get("System", {}).get("application", {}).get("applicationId")
        if skill != self._skill_id:
            raise AlexaError(f"the request is for skill {skill!r}")

        kind = request.get("type")
        intent = request.get("intent", {}).get("name") if kind == "IntentRequest" else None
        if kind == "LaunchRequest" or intent == "AMAZON.HelpIntent":
            return reply(HELP)
        if intent != ASK_INTENT:
            # Stop, cancel, home, the fallback, and the session ending.
            return reply(NOT_HEARD if intent == "AMAZON.FallbackIntent" else None)
        text = (request["intent"].get("slots", {}).get(ASK_SLOT, {}).get("value") or "").strip()
        if not text:
            return reply(NOT_HEARD)
        tv = self._tv()
        if tv is None:
            return reply(NO_TV)
        self._ask(tv, text)
        return reply()

    def _tv(self) -> Device | None:
        """The household TV that shows huvu now: one with a push stream
        open. With several, the one paired last."""
        accounts = self._kernel.accounts
        showing = {c["device"] for c in self._kernel.push.clients}
        tvs = [d for d in accounts.devices() if d.kind == "tv" and d.group == accounts.default_group() and d.id in showing]
        return max(tvs, key=lambda d: d.created_at, default=None)

    def _ask(self, tv: Device, text: str) -> None:
        """Asks in the background: a turn still running on the TV is
        stopped first, and Alexa waits 8 seconds at most."""

        async def ask() -> None:
            try:
                await self._kernel.conversations.ask(tv.id, text, user=None, group=tv.group, mode="voice")
            except Exception:
                log.exception("an ask from Alexa failed")

        task = asyncio.create_task(ask())
        self._asks.add(task)
        task.add_done_callback(self._asks.discard)


def _json(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except ValueError as err:
        raise AlexaError("the request isn't JSON") from err
    if not isinstance(value, dict):
        raise AlexaError("the request isn't a JSON object")
    return value
