"""Trigger scripts: a Python function the model writes, run when a trigger
matches, in place of a plain wake.

The script lives in a file and runs in its own process. It talks to the
kernel over its stdin and stdout, one JSON line per message, and calls the
same tools Hermes sees, limited to the ones its trigger declared in
`grants`. Actions still go through the policy. It is not sandboxed: the
agent already has a terminal, and it's there to help.

The engine keeps the watching, edges, rearm and expiry, so a script only
decides: act, wake the model, or do nothing. See script_host.py for the
side that runs inside the process.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .db import dumps
from .registry import suggest
from .spec import Caller

if TYPE_CHECKING:
    from .kernel import Kernel
    from .triggers import Trigger

log = logging.getLogger("huvu.scripts")

TIMEOUT = 30.0
# How much of a script's stderr the report keeps.
LOG_TAIL = 4000


class ScriptError(ValueError):
    pass


class ScriptRunner:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self._dir = kernel.config.data_dir / "scripts"
        self._dir.mkdir(parents=True, exist_ok=True)

    def check(self, script: str, grants: list[str]) -> None:
        """Fails on a syntax error, a missing `run`, or a grant that names no
        tool."""
        try:
            tree = ast.parse(script)
        except SyntaxError as err:
            raise ScriptError(f"script line {err.lineno}: {err.msg}") from err
        names = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if "run" not in names:
            raise ScriptError("the script must define run(ctx) or async run(ctx)")
        known = self._kernel.tools.names()
        for grant in grants:
            if grant not in known:
                raise ScriptError(f"grant {grant!r} names no tool{suggest(grant, known)}")

    async def run(self, trigger: Trigger, payload: dict[str, Any], dry: bool) -> dict[str, Any]:
        """Runs the trigger's script once. In a dry run, queries run for
        real, while actions and wakes are recorded and skipped."""
        path = self._dir / f"{trigger.id}.py"
        path.write_text(trigger.spec.script or "")
        caller = Caller(kind="script", trigger=trigger.id, user=trigger.user)
        report: dict[str, Any] = {"dry_run": dry, "calls": [], "wakes": [], "error": None, "log": ""}
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "huvu.script_host",
            str(path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(self._converse(proc, trigger, payload, caller, dry, report), TIMEOUT)
        except TimeoutError:
            proc.kill()
            report["error"] = f"timed out after {int(TIMEOUT)} s"
        finally:
            await proc.wait()
            stderr = await proc.stderr.read() if proc.stderr else b""
            report["log"] = stderr.decode(errors="replace")[-LOG_TAIL:]
        if not dry:
            await self._log(trigger, report)
        return report

    async def _converse(self, proc, trigger: Trigger, payload: dict[str, Any], caller: Caller, dry: bool, report: dict[str, Any]) -> None:
        assert proc.stdin and proc.stdout
        await _send(proc, {"payload": payload})
        while True:
            line = await proc.stdout.readline()
            if not line:
                report["error"] = report["error"] or "the script exited before it finished"
                break
            message = json.loads(line)
            if "done" in message:
                report["error"] = message.get("error")
                break
            reply = await self._answer(message, trigger, payload, caller, dry, report)
            await _send(proc, {"id": message.get("id"), **reply})

    async def _answer(self, message: dict[str, Any], trigger: Trigger, payload: dict[str, Any], caller: Caller, dry: bool, report: dict[str, Any]) -> dict[str, Any]:
        if "wake" in message:
            reply = await self._wake(message, trigger, payload, dry, report)
        else:
            reply = await self._call(message, trigger, caller, dry, report)
        return reply

    async def _wake(self, message: dict[str, Any], trigger: Trigger, payload: dict[str, Any], dry: bool, report: dict[str, Any]) -> dict[str, Any]:
        wake = {"text": message["wake"], "data": message.get("data")}
        report["wakes"].append(wake)
        if not dry:
            await self._kernel.waker.wake({**payload, "script": wake}, trigger.user)
        return {"result": {"woke": not dry}}

    async def _call(self, message: dict[str, Any], trigger: Trigger, caller: Caller, dry: bool, report: dict[str, Any]) -> dict[str, Any]:
        tool, args = message.get("call", ""), message.get("args") or {}
        record: dict[str, Any] = {"tool": tool, "args": args}
        report["calls"].append(record)
        if tool not in trigger.spec.grants:
            record["refused"] = "not in the trigger's grants"
            reply: dict[str, Any] = {"error": f"{tool} is not in this trigger's grants: {', '.join(trigger.spec.grants) or 'none'}"}
        elif dry and self._kernel.tools.writes(tool):
            record["skipped"] = "dry run"
            reply = {"result": {"dry_run": True}}
        else:
            try:
                result = await self._kernel.tools.call(tool, args, caller)
                reply = {"result": result.data}
            except Exception as err:
                record["error"] = str(err)
                reply = {"error": str(err)}
        return reply

    async def _log(self, trigger: Trigger, report: dict[str, Any]) -> None:
        if report["error"]:
            await self._kernel.kernel_event(
                "trigger.error",
                f"trigger {trigger.id}'s script failed: {report['error']}",
                entity=trigger.id,
                user=trigger.user,
                data={"log": report["log"][-500:]},
            )
        else:
            calls = ", ".join(c["tool"] for c in report["calls"]) or "no tools"
            await self._kernel.kernel_event(
                "trigger.script",
                f"trigger {trigger.id}'s script called {calls} and woke the model {len(report['wakes'])} times",
                entity=trigger.id,
                user=trigger.user,
            )

    def remove(self, trigger_id: str) -> None:
        Path(self._dir / f"{trigger_id}.py").unlink(missing_ok=True)


async def _send(proc, message: dict[str, Any]) -> None:
    proc.stdin.write((dumps(message) + "\n").encode())
    await proc.stdin.drain()
