from __future__ import annotations

import pytest

from huvu.speech import LONGEST, Voice, sentences

pytestmark = pytest.mark.usefixtures("family")


def test_a_sentence_waits_until_something_follows_it():
    assert sentences("On it.", 0, final=False) == ([], 0)
    ready, rest = sentences("On it. Setting", 0, final=False)
    assert ready == ["On it."] and rest == 6
    assert sentences("On it. Setting that up.", rest, final=True) == (["Setting that up."], 23)


def test_numbers_and_markdown_read_naturally():
    # "3." may yet become "3.5": nothing follows it, so it waits.
    assert sentences("It is 3.", 0, final=False) == ([], 0)
    assert sentences("It is 3.5 degrees.", 0, final=True)[0] == ["It is 3.5 degrees."]
    ready, _ = sentences("**Done.** See [the list](http://x.y/z).\n- one\n- two", 0, final=True)
    assert ready == ["Done.", "See the list.", "one", "two"]
    assert sentences("All set! \U0001F33C\u2600\ufe0f Talk soon.", 0, final=True)[0] == ["All set!", "Talk soon."]


def test_a_long_run_on_is_cut_at_a_comma():
    text = "word, " * 60
    ready, rest = sentences(text, 0, final=False)
    assert len(ready) == 1 and 0 < rest <= LONGEST


async def test_the_answer_is_spoken_a_sentence_at_a_time(kernel, monkeypatch):
    said: list[str] = []

    async def clip(self, text: str) -> bytes:
        said.append(text)
        return text.encode()

    monkeypatch.setattr(Voice, "clip", clip)

    async def events():
        yield "turn.started", {"ask": "hi"}
        yield "answer.delta", {"text": "On it, setting "}
        yield "answer.delta", {"text": "that up.\n\nDone. Every"}
        # Nothing is said for a part.
        yield "part", {"part": {}}
        yield "answer.delta", {"text": " day at 9."}
        yield "turn.ended", {"answer": "On it, setting that up.\n\nDone. Every day at 9."}

    audio = [chunk async for chunk in kernel.voice.speak(events())]
    assert said == ["On it, setting that up.", "Done.", "Every day at 9."]
    # Each clip framed by its length.
    assert audio == [len(s).to_bytes(4, "big") + s.encode() for s in said]


async def test_an_ask_goes_to_groq_with_the_familys_names(kernel):
    import httpx

    seen: dict[str, object] = {}

    def groq(request: httpx.Request) -> httpx.Response:
        body = request.content
        seen["auth"] = request.headers["authorization"]
        seen["body"] = body
        return httpx.Response(200, json={"text": " Remind Hrishik at nine. "})

    kernel.hearing._key = "gsk_test"
    kernel.hearing._http = httpx.AsyncClient(transport=httpx.MockTransport(groq))
    text = await kernel.hearing.transcribe(b"\x1aE\xdf\xa3 clip", "audio/webm;codecs=opus")
    assert text == "Remind Hrishik at nine."
    assert seen["auth"] == "Bearer gsk_test"
    body = seen["body"]
    assert b'filename="ask.webm"' in body and b"whisper-large-v3" in body and b"\r\n\r\nen\r\n" in body
    # The family's names steer the spelling.
    assert b"huvu, " in body and b"Mom" in body


async def test_no_key_means_no_hearing(kernel):
    from huvu.hearing import HearingError

    kernel.hearing._key = None
    assert not kernel.hearing.available()
    with pytest.raises(HearingError):
        await kernel.hearing.transcribe(b"clip", "audio/webm")


async def test_a_rate_limit_moves_to_the_other_model_then_says_so(kernel):
    import httpx

    from huvu.hearing import HearingError

    tried: list[bytes] = []
    limited = {"whisper-large-v3"}

    def groq(request: httpx.Request) -> httpx.Response:
        model = b"whisper-large-v3-turbo" if b"whisper-large-v3-turbo" in request.content else b"whisper-large-v3"
        tried.append(model)
        if model.decode() in limited:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"text": "turbo heard it"})

    kernel.hearing._key = "gsk_test"
    kernel.hearing._http = httpx.AsyncClient(transport=httpx.MockTransport(groq))
    assert await kernel.hearing.transcribe(b"clip", "audio/webm") == "turbo heard it"
    assert tried == [b"whisper-large-v3", b"whisper-large-v3-turbo"]
    limited.add("whisper-large-v3-turbo")
    with pytest.raises(HearingError) as err:
        await kernel.hearing.transcribe(b"clip", "audio/webm")
    assert err.value.limited

