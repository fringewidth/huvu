"""The HTTP server: the frontend's API, the MCP endpoint, and media.

The frontend contract is in docs/frontend-contract.md. Every device has one
conversation. `POST /api/chat` takes an ask and what the user did in the
app since the last one, and answers with an SSE stream of turn events.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.routing import Route

from . import mcp_server, prompts
from .accounts import AuthError, Device
from .auth import McpAuth, bearer, device_auth, ingest_auth, mcp_caller, phone_auth
from .config import Config
from .conversations import Action
from .hearing import HearingError
from .kernel import Kernel
from .push import api_json, sse
from .source import NotIngesting, SourceError
from .spec import Caller
from .speech import MEDIA_TYPE as SPEECH
from .times import for_model
from .tools import ToolError
from .triggers import TriggerError

log = logging.getLogger("huvu.http")

KEEPALIVE = 15.0


class Ask(BaseModel):
    ask: str
    # The client names its turn, so it can stop or rewind it by an id it
    # already has. The backend picks one when this is left out.
    id: str | None = Field(default=None, pattern=r"^[\w-]{1,64}$")
    # The turn this ask replaces, with everything after it (edit and retry).
    replaces: str | None = None
    # What the user did in the app since the last ask. Context for the
    # model, never a request.
    actions: list[Action] = Field(default_factory=list)
    # How the ask was made: spoken in voice mode, or typed.
    mode: Literal["voice", "text"] = "text"


class NewDevice(BaseModel):
    name: str = Field(max_length=60)
    kind: str


class Login(BaseModel):
    username: str
    password: str
    device: NewDevice


class PairCode(BaseModel):
    code: str
    # Which of the confirming member's groups the device joins. Needed only
    # when they're a member of more than one.
    group: str | None = None


class Answer(BaseModel):
    approve: bool


class Identify(BaseModel):
    user: str


class IdentifyAnswer(BaseModel):
    yes: bool


class PushToken(BaseModel):
    token: str = Field(min_length=1)
    platform: Literal["android", "ios"]


class Situation(BaseModel):
    values: list[str]


class Thinking(BaseModel):
    session: str
    text: str
    # True for the first piece of a burst of thinking.
    start: bool = False


def create_app(config: Config, kernel: Kernel | None = None) -> FastAPI:
    kernel = kernel or Kernel(config)
    accounts = kernel.accounts
    mcp = mcp_server.build(kernel)
    device = Depends(device_auth(config, accounts))
    phone = Depends(phone_auth(config, accounts))
    poster = Depends(ingest_auth(config, accounts))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await kernel.start()
        async with mcp.run():
            yield
        await kernel.stop()

    app = FastAPI(title="huvu", lifespan=lifespan)
    app.state.kernel = kernel
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
        # WHEP clients read their session's URL from Location.
        expose_headers=["Location"],
    )
    app.router.routes.append(Route("/mcp", endpoint=McpAuth(mcp.handle_request, config, accounts), methods=["GET", "POST", "DELETE"]))

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, err: AuthError) -> JSONResponse:
        return JSONResponse({"detail": str(err)}, status_code=err.status)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "sources": kernel.registry.names, "clients": kernel.push.clients}

    # Devices

    @app.post("/api/login")
    async def login(body: Login) -> JSONResponse:
        """A personal device (phone, web) logs in with its user's password."""
        try:
            new, token = accounts.login(body.username, body.password, body.device.name, body.device.kind)
        except AuthError as err:
            raise HTTPException(401, str(err)) from err
        return JSONResponse(api_json({"token": token, "device": new.public()}))

    @app.post("/api/pair")
    async def pair(body: NewDevice) -> JSONResponse:
        """A household device (TV, speaker) asks to pair. It shows `code`,
        and polls GET /api/pair/{pairing} until a phone confirms it."""
        try:
            pairing = accounts.start_pairing(body.name, body.kind)
        except AuthError as err:
            raise HTTPException(400, str(err)) from err
        return JSONResponse(api_json({"pairing": pairing.id, "code": f"{pairing.code[:4]}-{pairing.code[4:]}", "expires_at": pairing.expires_at}))

    @app.get("/api/pair/{pairing_id}")
    async def pairing(pairing_id: str) -> JSONResponse:
        try:
            collected = accounts.collect_pairing(pairing_id)
        except AuthError as err:
            raise HTTPException(410, str(err)) from err
        body: dict[str, Any] = {"status": "waiting"}
        status = 202
        if collected is not None:
            paired, token = collected
            body = {"status": "paired", "token": token, "device": paired.public()}
            status = 200
        return JSONResponse(api_json(body), status_code=status)

    @app.post("/api/pair/confirm")
    async def confirm(body: PairCode, by: Device = phone) -> JSONResponse:
        return JSONResponse(api_json(accounts.confirm_pairing(body.code, by, body.group).public()))

    @app.get("/api/me")
    async def me(me: Device = device) -> JSONResponse:
        user = accounts.user(me.user) if me.user else None
        groups = accounts.memberships(me.user) if me.user else {}
        return JSONResponse(api_json({"device": me.public(), "user": user.public() if user else None, "groups": groups}))

    @app.get("/api/devices")
    async def devices(me: Device = device) -> JSONResponse:
        """A user's own devices and their member groups' shared devices. On
        a shared device, only itself."""
        listed = accounts.devices_for(me.user) if me.user else [me]
        return JSONResponse(api_json([d.public() for d in listed]))

    @app.delete("/api/devices/{device_id}")
    async def revoke(device_id: str, me: Device = device) -> dict[str, bool]:
        """Logs a device out: one's own, or a shared device of a group one is
        a member of. A shared device can only unpair itself."""
        target = accounts.device(device_id)
        if target is None or not accounts.manages(me, target):
            raise HTTPException(404, f"no device {device_id!r}")
        accounts.revoke(device_id)
        return {"ok": True}

    # Who is at a shared device

    @app.get("/api/people")
    async def people(me: Device = device) -> JSONResponse:
        """Everyone in the device's group, for a shared device's "who's
        this?" picker."""
        return JSONResponse([u.public() for u in accounts.people(me.group or accounts.default_group())])

    @app.get("/api/attendance")
    async def attendance(me: Device = device) -> JSONResponse:
        """Who is recognized at this device. A personal device answers with
        its own user."""
        user = me.user or kernel.attendance.holder(me.id)
        named = accounts.user(user) if user else None
        return JSONResponse({"user": named.public() if named else None})

    @app.get("/api/attendance/mine")
    async def recognized(me: Device = device) -> JSONResponse:
        """The shared devices this device's user is recognized at, so their
        phone can end it from afar."""
        return JSONResponse(api_json(kernel.attendance.places(me.user) if me.user else []))

    @app.post("/api/attendance")
    async def identify(body: Identify, me: Device = device) -> JSONResponse:
        """A shared device asks: is this person here? Their phones get the
        question. Asking again while they haven't answered changes nothing
        and answers 409."""
        check = await kernel.attendance.ask(me.id, body.user, "device")
        if check is None:
            raise HTTPException(409, "they were already asked; waiting for their answer")
        return JSONResponse(api_json(check.public(me.name)))

    @app.delete("/api/attendance/{device_id}")
    async def release(device_id: str, me: Device = device) -> dict[str, bool]:
        """Ends who is recognized at a device: from the device itself, or the
        recognized person's phone."""
        ended = await kernel.attendance.release(device_id, me, "released from the app")
        return {"ok": ended is not None}

    @app.get("/api/identify")
    async def questions(by: Device = phone) -> JSONResponse:
        return JSONResponse(api_json(kernel.attendance.pending_for(by.user)))

    @app.post("/api/identify/{check_id}")
    async def answer_identify(check_id: str, body: IdentifyAnswer, by: Device = phone) -> JSONResponse:
        check = await kernel.attendance.answer(check_id, body.yes, by)
        return JSONResponse(api_json({"id": check.id, "status": check.status}))

    # The conversation

    @app.post("/api/chat")
    async def chat(ask: Ask, me: Device = device) -> StreamingResponse:
        live = await _ask(kernel, ask, me)
        events = kernel.conversations.subscribe(live)
        return StreamingResponse(_sse_stream(events), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.post("/api/turns/{turn_id}/stop")
    async def stop(turn_id: str, me: Device = device) -> dict[str, bool]:
        if not kernel.conversations.owns(me.id, turn_id):
            raise HTTPException(404, f"no turn {turn_id!r}")
        await kernel.conversations.stop(turn_id)
        return {"ok": True}

    @app.get("/api/turns/{turn_id}/events")
    async def turn_events(turn_id: str, me: Device = device) -> StreamingResponse:
        """One turn's events from its start, then live until it ends, for a
        client that opens while it runs."""
        if not kernel.conversations.owns(me.id, turn_id):
            raise HTTPException(404, f"no turn {turn_id!r}")
        events = kernel.conversations.replay(turn_id)
        return StreamingResponse(_sse_stream(events), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/speech")
    async def speech(me: Device = device) -> dict[str, bool]:
        """What huvu can do with sound here: hear (speech to text) and speak."""
        return {"listen": kernel.hearing.available(), "speak": True}

    @app.post("/api/transcribe")
    async def transcribe(request: Request, me: Device = device) -> dict[str, str]:
        """One recorded ask, as the request body, to text. The person at the
        device is named to Whisper, so their name comes out spelled right."""
        if not kernel.hearing.available():
            raise HTTPException(503, "huvu can't hear: GROQ_API_KEY isn't set")
        speaker = me.user or kernel.attendance.holder(me.id)
        try:
            text = await kernel.hearing.transcribe(await request.body(), request.headers.get("content-type", ""), speaker)
        except HearingError as err:
            raise HTTPException(429 if err.limited else 502, str(err)) from err
        return {"text": text}

    @app.get("/api/turns/{turn_id}/speech")
    async def turn_speech(turn_id: str, me: Device = device) -> StreamingResponse:
        """huvu saying a turn's answer while it writes it: one framed MP3
        clip per sentence, a sentence at a time (see
        speech.py). Works for a running turn and an ended one."""
        if not kernel.conversations.owns(me.id, turn_id):
            raise HTTPException(404, f"no turn {turn_id!r}")
        audio = kernel.voice.speak(kernel.conversations.replay(turn_id))
        return StreamingResponse(audio, media_type=SPEECH, headers={"Cache-Control": "no-cache"})

    @app.get("/api/conversation")
    async def conversation(me: Device = device) -> JSONResponse:
        return JSONResponse(api_json([t.public() for t in kernel.conversations.turns(me.id)]))

    @app.get("/api/push")
    async def push(me: Device = device) -> StreamingResponse:
        events = kernel.push.subscribe(me.id)
        return StreamingResponse(_sse_stream(events, forever=True), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.post("/api/push/token")
    async def push_token(body: PushToken, me: Device = phone) -> dict[str, bool]:
        """A phone's FCM token, so what needs its person reaches it while the
        app is closed. Logging the device out drops it."""
        kernel.native_push.register(me.id, body.token, body.platform)
        return {"ok": True}

    @app.get("/api/feeds", dependencies=[device])
    async def feeds() -> JSONResponse:
        return JSONResponse([f.model_dump() for f in await kernel.feeds()])

    @app.get("/api/media/{token}")
    async def media(token: str) -> Response:
        try:
            blob = await kernel.media.fetch(token)
        except SourceError as err:
            raise HTTPException(404, str(err)) from err
        return Response(blob.content, media_type=blob.content_type, headers={"Cache-Control": "private, max-age=86400"})

    # What waits on a phone

    @app.get("/api/approvals")
    async def approvals(by: Device = phone) -> JSONResponse:
        return JSONResponse(api_json([kernel.policy.public(a) for a in kernel.policy.pending() if kernel.policy.answerable(a, by)]))

    @app.post("/api/approvals/{approval_id}")
    async def answer(approval_id: str, body: Answer, by: Device = phone) -> JSONResponse:
        try:
            approval = await kernel.policy.resolve(approval_id, body.approve, by)
        except SourceError as err:
            raise HTTPException(409, str(err)) from err
        return JSONResponse(api_json(kernel.policy.public(approval)))

    @app.get("/api/notifications")
    async def notifications(by: Device = phone) -> JSONResponse:
        """The notifications of the last few days, for a phone that wasn't
        connected when they were sent."""
        return JSONResponse(api_json(kernel.notifications(by.user)))

    # Triggers

    @app.get("/api/triggers")
    async def triggers(me: Device = device) -> JSONResponse:
        """The triggers of this device's user, or of whoever is recognized at
        a shared device."""
        caller = Caller(kind="frontend", user=me.user or kernel.attendance.holder(me.id), device=me.id, group=me.group)
        return JSONResponse(api_json([t.public() for t in kernel.triggers.list(caller)]))

    @app.post("/api/triggers/{trigger_id}/{verb}")
    async def trigger_control(trigger_id: str, verb: str, me: Device = device) -> JSONResponse:
        """Pause or resume one of them. Resuming one the wake limit paused
        takes this: a person's tap. `approve` says yes to an advanced
        trigger that waits: on the person's own device the tap is the yes,
        on a shared one it asks their phone like any approval."""
        caller = Caller(kind="frontend", user=me.user or kernel.attendance.holder(me.id), device=me.id, group=me.group)
        if verb == "approve":
            trigger = kernel.triggers.get(trigger_id)
            if trigger is None or trigger not in kernel.triggers.list(caller):
                raise HTTPException(404, f"no trigger {trigger_id!r}")
            await kernel.policy.request("huvu", "activate_trigger", {"trigger": trigger_id}, caller)
            return JSONResponse(api_json(trigger.public()))
        controls = {"pause": kernel.triggers.pause, "resume": kernel.triggers.resume}
        if verb not in controls:
            raise HTTPException(404, f"no trigger control {verb!r}")
        try:
            trigger = await controls[verb](trigger_id, caller)
        except TriggerError as err:
            raise HTTPException(409, str(err)) from err
        return JSONResponse(api_json(trigger.public()))

    @app.post("/api/actions/{source}/{action}")
    async def act(source: str, action: str, request: Request, me: Device = device) -> JSONResponse:
        """Taps in the app. On a personal device the tap is the approval, so
        it runs at once. On a household device it goes through the policy."""
        args = await request.json() if await request.body() else {}
        caller = Caller(kind="frontend", user=me.user or kernel.attendance.holder(me.id), device=me.id, group=me.group)
        try:
            result = await kernel.policy.request(source, action, args, caller, reason=f"tapped on {me.name}")
        except (SourceError, ToolError) as err:
            raise HTTPException(400, str(err)) from err
        return JSONResponse(api_json(result.data))

    # Data devices post to sources

    @app.post("/api/ingest/{source}")
    async def ingest(source: str, request: Request, me: Device = poster) -> JSONResponse:
        """A device posts data of its own to a source, such as a phone's
        location from OwnTracks. The source's reply is the response body."""
        try:
            target = kernel.registry.get(source)
            body = await request.json() if await request.body() else None
            reply = await target.ingest(body, Caller(kind="device", user=me.user, device=me.id))
        except NotIngesting as err:
            raise HTTPException(404, str(err)) from err
        except json.JSONDecodeError as err:
            raise HTTPException(400, f"the body isn't JSON: {err}") from err
        except SourceError as err:
            raise HTTPException(404 if source not in kernel.registry.names else 400, str(err)) from err
        return JSONResponse(api_json(reply))

    # For the huvu Hermes plugin

    @app.post("/api/thinking")
    async def thinking(request: Request, body: Thinking) -> dict[str, bool]:
        """The model's reasoning summary as it streams, from the huvu Hermes
        plugin, for the stage to show while a turn runs."""
        known, user, group = mcp_caller(config, accounts, bearer(request))
        if not known:
            raise HTTPException(401, "unauthorized")
        return {"shown": kernel.conversations.think(body.session, body.text, body.start, user, group)}

    @app.post("/api/situation")
    async def situation(request: Request, body: Situation) -> dict[str, str]:
        """The situation block for the huvu Hermes plugin: chosen values,
        readable, and what waits on the profile's user. Which values go in
        is Hermes's choice; the plugin sends them."""
        known, user, group = mcp_caller(config, accounts, bearer(request))
        if not known:
            raise HTTPException(401, "unauthorized")
        readings = for_model(await kernel.state.get(body.values, accounts.scope(user, group)), kernel.now())
        helping = shared = None
        if user:
            named = accounts.user(user)
            helping = named.name if named else user
        else:
            group_named = accounts.group(group)
            people = ", ".join(f"{u.name} ({u.id})" for u in accounts.people(group or accounts.default_group()))
            shared = (group_named.name if group_named else group, people)
        values = [(key, r.get("value", r.get("reason")), r.get("at", "no time")) for key, r in readings.items()]
        pending = kernel.policy.pending_for(accounts.scope(user, group))
        return {"text": prompts.situation(f"{kernel.now():%a %Y-%m-%d %H:%M}", helping, shared, values, len(pending))}

    return app


async def _ask(kernel: Kernel, ask: Ask, me: Device):
    try:
        live = await kernel.conversations.ask(me.id, ask.ask, ask.replaces, user=me.user, group=me.group, actions=ask.actions, turn_id=ask.id, mode=ask.mode)
    except KeyError as err:
        raise HTTPException(404, str(err)) from err
    except ValueError as err:
        raise HTTPException(409, str(err)) from err
    return live


async def _sse_stream(events: AsyncIterator[tuple[str, Any]], forever: bool = False) -> AsyncIterator[str]:
    """Events as SSE, with a keepalive comment when nothing was sent for a
    while, so proxies and clients don't time out."""
    queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for item in events:
                await queue.put(item)
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), KEEPALIVE)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item is None:
                break
            yield sse(*item)
    finally:
        task.cancel()
