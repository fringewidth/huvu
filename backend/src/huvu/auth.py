"""Auth: the frontend's bearer JWT, and Hermes's token for /mcp.

huvu has one user. A JWT is accepted only if it's signed with
HUVU_JWT_SECRET and its `email` (or `sub`) is the configured owner. With
`auth.mode = "off"`, every request passes, which is only for development
on localhost.
"""

from __future__ import annotations

import hmac

import jwt
from fastapi import HTTPException, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Config


def require_owner(config: Config):
    def check(request: Request) -> None:
        if config.auth_mode == "off":
            return
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else request.query_params.get("token", "")
        try:
            claims = jwt.decode(token, config.jwt_secret, algorithms=["HS256"])
        except jwt.PyJWTError as err:
            raise HTTPException(401, "invalid token") from err
        if (claims.get("email") or claims.get("sub")) != config.owner:
            raise HTTPException(403, "not the owner")

    return check


class McpAuth:
    """Wraps the MCP app: a bearer token equal to HUVU_MCP_TOKEN, when set."""

    def __init__(self, app: ASGIApp, token: str | None) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self._token:
            headers = dict(scope.get("headers") or [])
            given = headers.get(b"authorization", b"").decode().removeprefix("Bearer ").strip()
            if not hmac.compare_digest(given, self._token):
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self._app(scope, receive, send)
