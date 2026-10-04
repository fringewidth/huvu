"""Configuration: one TOML file, with secrets from the environment.

    data_dir = "data"
    public_url = "http://localhost:3001"
    port = 3001
    cors_origins = ["*"]

    [auth]
    mode = "on"              # or "off", for development on localhost
    dev_user = "you"         # with mode = "off", whose requests they are

    [hermes]
    url = "http://localhost:8642"

    [policy]
    "ha.turn_on" = "silent"  # source.action = silent | notify | ask

    [[sources]]
    name = "tuya"
    driver = "huvu_drivers.tuya:TuyaSource"
    options = { device_id = "..." }

Secrets never go in the file. HERMES_API_KEY (the default Hermes profile's
key) and HUVU_MCP_TOKEN (the token that profile sends to /mcp) come from the
environment; both serve household devices. Users, devices and each user's
profile secrets live in the database (see accounts.py), managed with
`huvu user ...`.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .spec import Autonomy


@dataclass
class SourceConfig:
    name: str
    driver: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    data_dir: Path = Path("data")
    public_url: str = "http://localhost:3001"
    host: str = "0.0.0.0"
    port: int = 3001
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    auth_mode: Literal["on", "off"] = "on"
    # With auth off, the user behind every request. None makes them the
    # household's.
    dev_user: str | None = None
    # The token Hermes's default profile (the household's) sends to /mcp.
    mcp_token: str | None = None
    hermes_url: str = "http://localhost:8642"
    # The default profile's API key.
    hermes_api_key: str | None = None
    policy: dict[str, Autonomy] = field(default_factory=dict)
    sources: list[SourceConfig] = field(default_factory=list)


def load(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("HUVU_CONFIG", "huvu.toml"))
    raw: dict[str, Any] = tomllib.loads(path.read_text()) if path.exists() else {}
    auth = raw.get("auth", {})
    hermes = raw.get("hermes", {})
    base = path.parent if path.exists() else Path.cwd()
    config = Config(
        data_dir=(base / raw.get("data_dir", "data")).resolve(),
        public_url=raw.get("public_url", "http://localhost:3001").rstrip("/"),
        host=raw.get("host", "0.0.0.0"),
        port=int(raw.get("port", 3001)),
        cors_origins=list(raw.get("cors_origins", ["*"])),
        auth_mode=auth.get("mode", "on"),
        dev_user=auth.get("dev_user"),
        mcp_token=os.environ.get("HUVU_MCP_TOKEN"),
        hermes_url=hermes.get("url", "http://localhost:8642"),
        hermes_api_key=os.environ.get("HERMES_API_KEY"),
        policy=dict(raw.get("policy", {})),
        sources=[SourceConfig(s["name"], s["driver"], dict(s.get("options", {}))) for s in raw.get("sources", [])],
    )
    if config.auth_mode not in ("on", "off"):
        raise ValueError(f"auth.mode must be on or off, not {config.auth_mode!r}")
    for key, level in config.policy.items():
        if level not in ("silent", "notify", "ask"):
            raise ValueError(f"policy {key!r}: {level!r} must be silent, notify or ask")
    return config
