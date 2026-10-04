"""The HTTP server: the frontend's API, the MCP endpoint, and media.

The frontend contract is in docs/frontend-contract.md. `POST /api/chat`
takes a conversation, an ask and what the owner did in the app since the
last one, and answers with an SSE stream of turn events.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.routing import Route

from . import mcp_server
from .auth import McpAuth, require_owner
from .config import Config
from .conversations import Action
from .kernel import Kernel
from .policy import Caller
from .push import api_json, sse
from .source import SourceError
from .times import for_model
from .tools import ToolError

log = logging.getLogger("huvu.http")

KEEPALIVE = 15.0
DEFAULT_CONVERSATION = "default"


class Ask(BaseModel):
    conversation: str = DEFAULT_CONVERSATION
    ask: str
    # The client names its turn, so it can stop or rewind it by an id it
    # already has. The backend picks one when this is left out.
    id: str | None = Field(default=None, pattern=r"^[\w-]{1,64}$")
    # The turn this ask replaces, with everything after it (edit and retry).
    replaces: str | None = None
    # What the owner did in the app since the last ask. Context for the
    # model, never a request.
    actions: list[Action] = Field(default_factory=list)


class Answer(BaseModel):
    approve: bool


class Situation(BaseModel):
    values: list[str]


def create_app(config: Config, kernel: Kernel | None = None) -> FastAPI:
    kernel = kernel or Kernel(config)
    mcp = mcp_server.build(kernel)
    owner = Depends(require_owner(config))

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
    app.router.routes.append(Route("/mcp", endpoint=McpAuth(mcp.handle_request, config.mcp_token), methods=["GET", "POST", "DELETE"]))

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "sources": kernel.registry.names, "clients": kernel.push.clients}

    @app.post("/api/chat", dependencies=[owner])
    async def chat(ask: Ask) -> StreamingResponse:
        live = await _ask(kernel, ask)
        events = kernel.conversations.subscribe(live)
        return StreamingResponse(_sse_stream(events), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.post("/api/turns/{turn_id}/stop", dependencies=[owner])
    async def stop(turn_id: str) -> dict[str, bool]:
        await kernel.conversations.stop(turn_id)
        return {"ok": True}

    @app.get("/api/conversations/{conversation_id}", dependencies=[owner])
    async def conversation(conversation_id: str) -> JSONResponse:
        return JSONResponse(api_json([t.public() for t in kernel.conversations.turns(conversation_id)]))

    @app.get("/api/push", dependencies=[owner])
    async def push(client: str | None = None, kind: str = "web") -> StreamingResponse:
        events = kernel.push.subscribe(client or secrets.token_hex(4), kind)
        return StreamingResponse(_sse_stream(events, forever=True), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/feeds", dependencies=[owner])
    async def feeds() -> JSONResponse:
        return JSONResponse([f.model_dump() for f in await kernel.feeds()])

    @app.get("/api/media/{token}")
    async def media(token: str) -> Response:
        try:
            blob = await kernel.media.fetch(token)
        except SourceError as err:
            raise HTTPException(404, str(err)) from err
        return Response(blob.content, media_type=blob.content_type, headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/approvals", dependencies=[owner])
    async def approvals() -> JSONResponse:
        return JSONResponse(api_json([a.public() for a in kernel.policy.pending()]))

    @app.post("/api/approvals/{approval_id}", dependencies=[owner])
    async def answer(approval_id: str, body: Answer) -> JSONResponse:
        try:
            approval = await kernel.policy.resolve(approval_id, body.approve)
        except SourceError as err:
            raise HTTPException(409, str(err)) from err
        return JSONResponse(api_json(approval.public()))

    @app.post("/api/actions/{source}/{action}", dependencies=[owner])
    async def act(source: str, action: str, request: Request) -> JSONResponse:
        """The owner's own taps. The tap is the approval, so these run at once."""
        args = await request.json() if await request.body() else {}
        try:
            result = await kernel.policy.request(source, action, args, Caller(kind="frontend"), reason="the owner tapped it")
        except (SourceError, ToolError) as err:
            raise HTTPException(400, str(err)) from err
        return JSONResponse(api_json(result.data))

    @app.post("/api/situation")
    async def situation(request: Request, body: Situation) -> dict[str, str]:
        """The situation block for the huvu Hermes plugin: chosen values,
        readable. Which values go in is Hermes's choice; the plugin sends
        them."""
        if config.mcp_token and request.headers.get("authorization", "") != f"Bearer {config.mcp_token}":
            raise HTTPException(401, "unauthorized")
        readings = for_model(await kernel.state.get(body.values), kernel.now())
        lines = [f"Now: {kernel.now():%a %Y-%m-%d %H:%M}"]
        lines += [f"{key} = {r.get('value', r.get('reason'))} ({r.get('at', 'no time')})" for key, r in readings.items()]
        pending = kernel.policy.pending()
        lines.append(f"Pending approvals: {len(pending)}" if pending else "Pending approvals: none")
        return {"text": "\n".join(lines)}

    return app


async def _ask(kernel: Kernel, ask: Ask):
    try:
        live = await kernel.conversations.ask(ask.conversation, ask.ask, ask.replaces, actions=ask.actions, turn_id=ask.id)
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
