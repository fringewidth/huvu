"""The HTTP server: the frontend's API, the MCP endpoint, and media.

The frontend contract is in docs/frontend-contract.md. Every device has one
conversation. `POST /api/chat` takes an ask and what the user did in the
app since the last one, and answers with an SSE stream of turn events.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.routing import Route

from . import mcp_server
from .accounts import AuthError, Device
from .auth import McpAuth, bearer, device_auth, mcp_caller, phone_auth
from .config import Config
from .conversations import Action
from .kernel import Kernel
from .push import api_json, sse
from .source import SourceError
from .spec import Caller
from .times import for_model
from .tools import ToolError

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


class Situation(BaseModel):
    values: list[str]


def create_app(config: Config, kernel: Kernel | None = None) -> FastAPI:
    kernel = kernel or Kernel(config)
    accounts = kernel.accounts
    mcp = mcp_server.build(kernel)
    device = Depends(device_auth(config, accounts))
    phone = Depends(phone_auth(config, accounts))

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
        return JSONResponse({"detail": str(err)}, status_code=403)

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

    @app.post("/api/attendance")
    async def identify(body: Identify, me: Device = device) -> JSONResponse:
        """A shared device asks: is this person here? Their phones get the
        question."""
        check = await kernel.attendance.ask(me.id, body.user, "device")
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

    @app.get("/api/conversation")
    async def conversation(me: Device = device) -> JSONResponse:
        return JSONResponse(api_json([t.public() for t in kernel.conversations.turns(me.id)]))

    @app.get("/api/push")
    async def push(me: Device = device) -> StreamingResponse:
        events = kernel.push.subscribe(me.id)
        return StreamingResponse(_sse_stream(events, forever=True), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

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
        return JSONResponse(api_json([a.public() for a in kernel.policy.pending() if kernel.policy.answerable(a, by)]))

    @app.post("/api/approvals/{approval_id}")
    async def answer(approval_id: str, body: Answer, by: Device = phone) -> JSONResponse:
        try:
            approval = await kernel.policy.resolve(approval_id, body.approve, by)
        except SourceError as err:
            raise HTTPException(409, str(err)) from err
        return JSONResponse(api_json(approval.public()))

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

    # For the huvu Hermes plugin

    @app.post("/api/situation")
    async def situation(request: Request, body: Situation) -> dict[str, str]:
        """The situation block for the huvu Hermes plugin: chosen values,
        readable, and what waits on the profile's user. Which values go in
        is Hermes's choice; the plugin sends them."""
        known, user, group = mcp_caller(config, accounts, bearer(request))
        if not known:
            raise HTTPException(401, "unauthorized")
        readings = for_model(await kernel.state.get(body.values), kernel.now())
        lines = [f"Now: {kernel.now():%a %Y-%m-%d %H:%M}"]
        if user:
            named = accounts.user(user)
            lines.append(f"You are helping: {named.name if named else user}")
        else:
            named = accounts.group(group)
            people = ", ".join(f"{u.name} ({u.id})" for u in accounts.people(group or accounts.default_group()))
            lines.append(
                f"You are serving {named.name if named else group} from a shared device; nobody in particular is speaking. "
                f"The people who use it: {people}. If someone says who they are, call people_identify."
            )
        lines += [f"{key} = {r.get('value', r.get('reason'))} ({r.get('at', 'no time')})" for key, r in readings.items()]
        pending = kernel.policy.pending_for(accounts.scope(user, group))
        lines.append(f"Pending approvals: {len(pending)}" if pending else "Pending approvals: none")
        return {"text": "\n".join(lines)}

    return app


async def _ask(kernel: Kernel, ask: Ask, me: Device):
    try:
        live = await kernel.conversations.ask(me.id, ask.ask, ask.replaces, user=me.user, group=me.group, actions=ask.actions, turn_id=ask.id)
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
