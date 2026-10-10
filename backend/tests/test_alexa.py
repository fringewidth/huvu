"""Asks through Alexa: Amazon's signature and certificate checks, and an ask
reaching the household TV that shows huvu."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from huvu import prompts
from huvu.alexa import NO_TV, Alexa, AlexaError, Verifier, chain_url_ok

SKILL = "amzn1.ask.skill.test"
CHAIN_URL = "https://s3.amazonaws.com/echo.api/echo-api-cert.pem"
NOW = datetime.now(timezone.utc)


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def _cert(subject: str, key, issuer: str, issuer_key, *, ca: bool, san: str | None = None, days: int = 30) -> x509.Certificate:
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
    )
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
    else:
        builder = builder.add_extension(
            x509.KeyUsage(True, False, False, False, False, False, False, False, False),
            critical=True,
        )
        builder = builder.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(san or "echo-api.amazon.com")]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


class Amazon:
    """A stand-in for Amazon: a root, a signing certificate under it, and
    requests signed the way Alexa signs them."""

    def __init__(self, san: str = "echo-api.amazon.com", days: int = 30) -> None:
        self.root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.root = _cert("test root", self.root_key, "test root", self.root_key, ca=True, days=3650)
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.leaf = _cert(san, self.key, "test root", self.root_key, ca=False, san=san, days=days)

    def verifier(self) -> Verifier:
        async def fetch(url: str) -> bytes:
            return self.leaf.public_bytes(serialization.Encoding.PEM)

        return Verifier(fetch=fetch, roots=[self.root])

    def headers(self, body: bytes) -> dict[str, str]:
        signature = self.key.sign(body, padding.PKCS1v15(), hashes.SHA256())
        return {"SignatureCertChainUrl": CHAIN_URL, "Signature-256": base64.b64encode(signature).decode()}


def envelope(text: str | None = "what time is it", *, skill: str = SKILL, at: datetime | None = None, kind: str = "IntentRequest") -> bytes:
    request: dict = {"type": kind, "requestId": "r1", "timestamp": (at or NOW).isoformat().replace("+00:00", "Z")}
    if kind == "IntentRequest":
        request["intent"] = {"name": "AskIntent", "slots": {"ask": {"name": "ask", "value": text}}}
    body = {
        "version": "1.0",
        "context": {"System": {"application": {"applicationId": skill}, "device": {"deviceId": "amzn1.ask.device.x"}}},
        "request": request,
    }
    return json.dumps(body).encode()


def test_only_amazons_chain_urls_pass():
    assert chain_url_ok("https://s3.amazonaws.com/echo.api/echo-api-cert.pem")
    assert chain_url_ok("HTTPS://s3.amazonaws.com/echo.api/echo-api-cert.pem")
    assert chain_url_ok("https://s3.amazonaws.com:443/echo.api/echo-api-cert.pem")
    assert chain_url_ok("https://s3.amazonaws.com/echo.api/../echo.api/echo-api-cert.pem")
    assert not chain_url_ok("http://s3.amazonaws.com/echo.api/echo-api-cert.pem")
    assert not chain_url_ok("https://notamazon.com/echo.api/echo-api-cert.pem")
    assert not chain_url_ok("https://s3.amazonaws.com/EcHo.aPi/echo-api-cert.pem")
    assert not chain_url_ok("https://s3.amazonaws.com/invalid.path/echo-api-cert.pem")
    assert not chain_url_ok("https://s3.amazonaws.com:563/echo.api/echo-api-cert.pem")
    assert not chain_url_ok("https://s3.amazonaws.com/echo.api/../evil/cert.pem")


async def test_a_signed_request_passes_and_a_tampered_one_doesnt():
    amazon = Amazon()
    verifier = amazon.verifier()
    body = envelope()
    headers = amazon.headers(body)
    await verifier.verify(headers["SignatureCertChainUrl"], headers["Signature-256"], body, NOW)
    with pytest.raises(AlexaError, match="signature"):
        await verifier.verify(headers["SignatureCertChainUrl"], headers["Signature-256"], body + b" ", NOW)


async def test_a_chain_for_another_name_or_root_is_refused():
    wrong_name = Amazon(san="example.com")
    body = envelope()
    headers = wrong_name.headers(body)
    with pytest.raises(AlexaError, match="isn't valid"):
        await wrong_name.verifier().verify(CHAIN_URL, headers["Signature-256"], body, NOW)

    amazon, stranger = Amazon(), Amazon()

    async def fetch(url: str) -> bytes:
        return amazon.leaf.public_bytes(serialization.Encoding.PEM)

    with pytest.raises(AlexaError, match="isn't valid"):
        await Verifier(fetch=fetch, roots=[stranger.root]).verify(CHAIN_URL, amazon.headers(body)["Signature-256"], body, NOW)


async def test_an_expired_chain_is_refused():
    amazon = Amazon(days=1)
    body = envelope()
    with pytest.raises(AlexaError, match="isn't valid"):
        await amazon.verifier().verify(CHAIN_URL, amazon.headers(body)["Signature-256"], body, NOW + timedelta(days=2))


async def _showing(kernel, device_id: str) -> asyncio.Task:
    """A push stream held open, the way the TV holds one while it shows
    huvu."""
    stream = kernel.push.subscribe(device_id)
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    return task


async def test_an_ask_goes_to_the_tv_showing_huvu(kernel, family, hermes):
    amazon = Amazon()
    alexa = Alexa(kernel, SKILL, amazon.verifier())
    body = envelope("what time is it")

    assert (await alexa.handle(amazon.headers(body), body))["response"]["outputSpeech"]["text"] == NO_TV

    showing = await _showing(kernel, family.tv.id)
    answer = await alexa.handle(amazon.headers(body), body)
    assert answer == {"version": "1.0", "response": {"shouldEndSession": True}}
    await asyncio.gather(*alexa._asks)
    [turn] = kernel.conversations.turns(family.tv.id)
    assert turn.ask == "what time is it"
    assert any(message.startswith(prompts.mode_note("voice")) and message.endswith("what time is it") for _, message in hermes.messages)
    showing.cancel()


async def test_requests_for_another_skill_or_too_old_are_refused(kernel, family):
    amazon = Amazon()
    alexa = Alexa(kernel, SKILL, amazon.verifier())
    other = envelope(skill="amzn1.ask.skill.other")
    with pytest.raises(AlexaError, match="skill"):
        await alexa.handle(amazon.headers(other), other)
    old = envelope(at=NOW - timedelta(minutes=5))
    with pytest.raises(AlexaError, match="too old"):
        await alexa.handle(amazon.headers(old), old)
    assert kernel.conversations.turns(family.tv.id) == []


async def test_alexa_hears_nothing_or_ends_without_an_ask(kernel, family):
    amazon = Amazon()
    alexa = Alexa(kernel, SKILL, amazon.verifier())
    empty = envelope(None)
    assert "outputSpeech" in (await alexa.handle(amazon.headers(empty), empty))["response"]
    ended = envelope(kind="SessionEndedRequest")
    assert (await alexa.handle(amazon.headers(ended), ended))["response"] == {"shouldEndSession": True}
    assert kernel.conversations.turns(family.tv.id) == []
