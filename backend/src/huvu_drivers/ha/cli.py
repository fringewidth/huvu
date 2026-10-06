"""Tools for setting Home Assistant up for huvu, run from backend/:

    uv run python -m huvu_drivers.ha.cli ws '{"type": "config/label_registry/list"}'
    uv run python -m huvu_drivers.ha.cli check

`ws` sends one WebSocket command and prints the result as JSON. `check`
runs the real driver once against HA, in a throwaway data folder, and
prints what huvu would see: the exposed entities, the actions with their
autonomy, and every setup problem. It exits with 1 when it can't connect
or finds a problem.

HA_URL (default http://homeassistant.local:8123) and HA_TOKEN come from the
environment, or else from backend/.env. `check` takes the `users` map from
the ha source in huvu.toml, if there is one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

from huvu.config import Config, secret
from huvu.config import load as load_config

from .client import HAClient
from .source import HomeAssistant

DEFAULT_URL = "http://homeassistant.local:8123"


def _url() -> str:
    return secret("HA_URL") or DEFAULT_URL


def _token() -> str:
    token = secret("HA_TOKEN")
    if not token:
        sys.exit("HA_TOKEN is not set (environment or backend/.env)")
    return token


async def ws(command: str) -> None:
    message = json.loads(command)
    client = await HAClient.open(_url(), _token())
    listener = asyncio.create_task(client.listen())
    try:
        result = await client.call(message.pop("type"), **message)
        print(json.dumps(result, indent=1, ensure_ascii=False))
    finally:
        listener.cancel()
        await client.close()


async def check() -> int:
    from huvu.kernel import Kernel

    options = {"url": _url()}
    configured = next((s for s in load_config().sources if s.driver.endswith(":HomeAssistant")), None)
    if configured is not None:
        options = {**configured.options, "url": configured.options.get("url", _url())}
    with tempfile.TemporaryDirectory() as folder:
        source = HomeAssistant(options)
        kernel = Kernel(Config(data_dir=Path(folder)), sources={"ha": source})
        task = asyncio.create_task(source.run())
        for _ in range(200):
            if source._connected or task.done():
                break
            await asyncio.sleep(0.05)
        code = _report(kernel, source, task)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await kernel.hermes.close()
        kernel.data.close()
        kernel.db.close()
    return code


def _report(kernel, source: HomeAssistant, task: asyncio.Task[None]) -> int:
    code = 1
    if not source._connected:
        error = task.exception() if task.done() else "timed out after 10 s"
        print(f"not connected: {error}")
    else:
        domains = Counter(e.partition(".")[0] for e in source._exposed)
        print(f"Home Assistant {source._client.version if source._client else '?'}")
        print(f"exposed entities: {len(source._exposed)} ({', '.join(f'{d} {n}' for d, n in sorted(domains.items()))})")
        print("actions:")
        for spec in kernel.registry.spec("ha").actions:
            level = "requires a person's tap" if spec.requires_human else spec.autonomy
            print(f"  ha_{spec.name} [{level}]: {spec.description}")
        print("problems:" if source._problems else "problems: none")
        for problem in source._problems:
            print(f"  {problem}")
        code = 1 if source._problems else 0
    return code


def main() -> None:
    parser = argparse.ArgumentParser(prog="huvu_drivers.ha.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    sent = commands.add_parser("ws", help="send one WebSocket command and print the result")
    sent.add_argument("message", help='JSON, such as \'{"type": "get_states"}\'')
    commands.add_parser("check", help="run the driver once and print what huvu sees")
    args = parser.parse_args()
    if args.command == "ws":
        asyncio.run(ws(args.message))
    else:
        sys.exit(asyncio.run(check()))


if __name__ == "__main__":
    main()
