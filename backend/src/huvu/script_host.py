"""The inside of a trigger script's process. Run as
`python -m huvu.script_host <file>` by scripts.py, never by hand.

It reads the trigger's payload from the first line of stdin, loads the
script, and calls its `run(ctx)`. Everything the script asks for goes out
on stdout as one JSON line, and the answer comes back on stdin. `print`
goes to stderr, which the kernel keeps as the script's log.

A script looks like this:

    def run(ctx):
        for press in ctx.rows:
            face = ctx.call("immich_match_face", {"ref": press["snapshot"]})
            if face.get("name"):
                ctx.call("huvu_notify", {"text": f"{face['name']} is at the door", "image": press["snapshot"]})
            else:
                ctx.wake("Unknown person at the door", {"snapshot": press["snapshot"]})
"""

from __future__ import annotations

import asyncio
import inspect
import json
import runpy
import sys
import traceback
from typing import Any


class ScriptCallError(Exception):
    pass


class Context:
    def __init__(self, payload: dict[str, Any], out) -> None:
        self.payload = payload
        self.note: str = payload.get("note", "")
        # The rows that fired the trigger, as dicts by column.
        self.rows: list[dict[str, Any]] = payload.get("rows", [])
        self.columns: list[str] = payload.get("columns", [])
        self._out = out
        self._next = 0

    def _ask(self, message: dict[str, Any]) -> Any:
        self._next += 1
        self._out.write(json.dumps({"id": self._next, **message}) + "\n")
        self._out.flush()
        reply = json.loads(sys.stdin.readline())
        if "error" in reply:
            raise ScriptCallError(reply["error"])
        return reply.get("result")

    def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        """Calls one of the trigger's granted tools and returns its data."""
        return self._ask({"call": tool, "args": args or {}})

    def wake(self, text: str, data: Any = None) -> None:
        """Wakes the model with the trigger's payload, this text and data."""
        self._ask({"wake": text, "data": data})

    def log(self, *parts: Any) -> None:
        print(*parts, file=sys.stderr, flush=True)


def main() -> None:
    out = sys.stdout
    sys.stdout = sys.stderr
    payload = json.loads(sys.stdin.readline())["payload"]
    ctx = Context(payload, out)
    error = None
    try:
        module = runpy.run_path(sys.argv[1], run_name="huvu_script")
        result = module["run"](ctx)
        if inspect.isawaitable(result):
            asyncio.run(_wait(result))
    except Exception as err:
        traceback.print_exc()
        error = f"{type(err).__name__}: {err}"
    out.write(json.dumps({"done": True, "error": error}) + "\n")
    out.flush()


async def _wait(awaitable) -> None:
    await awaitable


if __name__ == "__main__":
    main()
