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

    [models]                 # the three tiers of model huvu runs on
    basic = "gpt-6-luna"     # trigger wakes, unless the trigger asks for more
    pro = "gpt-6-sol"        # conversations, and wakes that need more thought
    advanced = "gpt-6-astra" # wakes only, and only with the person's yes

    [dream]
    at = "04:00"             # each person's nightly dream, local time
    tier = "pro"

    [speech]
    engine = "edge"          # edge (Microsoft's voices, online) or kokoro (local)
    voice = "en-IN-NeerjaExpressiveNeural"   # an Edge voice, or a Kokoro one such as af_heart
    lang = "en-us"           # Kokoro only: how text is read, en-us or en-gb
    speed = 1.0
    stt_model = "whisper-large-v3"   # on Groq, with GROQ_API_KEY

    [[sources]]
    name = "tuya"
    driver = "huvu_drivers.tuya:TuyaSource"
    options = { device_id = "..." }

Secrets never go in the file. HERMES_API_KEY (the default Hermes profile's
key) and HUVU_MCP_TOKEN (the token that profile sends to /mcp) come from the
environment, or else from backend/.env (gitignored), which setup.sh writes;
both serve the default group's shared devices. HUVU_FCM_CREDENTIALS, the
path of a Firebase service account's JSON key, turns on native push.
GROQ_API_KEY turns on speech to text (hearing.py). Users, devices and each user's
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

# The repo this backend runs from: backend/src/huvu/config.py, three up.
REPO = Path(__file__).resolve().parents[3]
# The model behind each tier, on the ChatGPT login Hermes uses.
DEFAULT_MODELS = {"basic": "gpt-6-luna", "pro": "gpt-6-sol", "advanced": "gpt-6-astra"}
# Secrets setup.sh keeps, for when the environment doesn't have them.
ENV_FILE = REPO / "backend" / ".env"


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
    # The path of a Firebase service account's JSON key, for native push.
    # None turns native push off.
    fcm_credentials: Path | None = None
    policy: dict[str, Autonomy] = field(default_factory=dict)
    sources: list[SourceConfig] = field(default_factory=list)
    # Where huvu writes a Hermes skill per source. Defaults to
    # <data_dir>/skills.
    skills_dir: Path | None = None
    # The folder every Hermes profile reads through skills.external_dirs.
    # huvu links each skill into it at every start. None skips the links.
    hermes_skills_dir: Path | None = None
    # huvu's voice (speech.py): a Kokoro voice, how its text is read, and
    # how fast. A Hindi voice such as hf_alpha reads English with en-us.
    speech_engine: str = "edge"
    # None: the engine's default (speech.DEFAULT_VOICES).
    speech_voice: str | None = None
    speech_lang: str = "en-us"
    speech_speed: float = 1.0
    # The model behind each tier (Tier in spec.py). Conversations run on
    # pro; a trigger's wakes on its own tier, basic by default.
    models: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODELS))
    # Each person's nightly dream (dream.py): when, and on which tier.
    dream_at: str = "04:00"
    dream_tier: str = "pro"
    # Speech to text on Groq (hearing.py). No key, no hearing.
    groq_api_key: str | None = None
    stt_model: str = "whisper-large-v3"

    def model(self, tier: str) -> str:
        return self.models.get(tier) or DEFAULT_MODELS[tier]

    @property
    def skills_folder(self) -> Path:
        return self.skills_dir or self.data_dir / "skills"


def load(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("HUVU_CONFIG", "huvu.toml"))
    raw: dict[str, Any] = tomllib.loads(path.read_text()) if path.exists() else {}
    auth = raw.get("auth", {})
    hermes = raw.get("hermes", {})
    speech = raw.get("speech", {})
    dream = raw.get("dream", {})
    base = path.parent if path.exists() else Path.cwd()
    fcm = secret("HUVU_FCM_CREDENTIALS")
    config = Config(
        data_dir=(base / raw.get("data_dir", "data")).resolve(),
        public_url=raw.get("public_url", "http://localhost:3001").rstrip("/"),
        host=raw.get("host", "0.0.0.0"),
        port=int(raw.get("port", 3001)),
        cors_origins=list(raw.get("cors_origins", ["*"])),
        auth_mode=auth.get("mode", "on"),
        dev_user=auth.get("dev_user"),
        mcp_token=secret("HUVU_MCP_TOKEN"),
        hermes_url=hermes.get("url", "http://localhost:8642"),
        hermes_api_key=secret("HERMES_API_KEY"),
        fcm_credentials=Path(fcm).expanduser() if fcm else None,
        policy=dict(raw.get("policy", {})),
        skills_dir=(base / raw["skills_dir"]).resolve() if raw.get("skills_dir") else None,
        hermes_skills_dir=(base / raw["hermes_skills_dir"]).resolve() if raw.get("hermes_skills_dir") else REPO / "hermes-skills",
        models={**DEFAULT_MODELS, **raw.get("models", {})},
        dream_at=str(dream.get("at", "04:00")),
        dream_tier=str(dream.get("tier", "pro")),
        speech_engine=speech.get("engine", "edge"),
        speech_voice=speech.get("voice"),
        speech_lang=speech.get("lang", "en-us"),
        speech_speed=float(speech.get("speed", 1.0)),
        groq_api_key=secret("GROQ_API_KEY"),
        stt_model=speech.get("stt_model", "whisper-large-v3"),
        sources=[SourceConfig(s["name"], s["driver"], dict(s.get("options", {}))) for s in raw.get("sources", [])],
    )
    for tier in config.models:
        if tier not in DEFAULT_MODELS:
            raise ValueError(f"models.{tier}: the tiers are {', '.join(DEFAULT_MODELS)}")
    if config.dream_tier not in DEFAULT_MODELS:
        raise ValueError(f"dream.tier must be one of {', '.join(DEFAULT_MODELS)}")
    if config.speech_engine not in ("edge", "kokoro"):
        raise ValueError(f"speech.engine must be edge or kokoro, not {config.speech_engine!r}")
    if config.auth_mode not in ("on", "off"):
        raise ValueError(f"auth.mode must be on or off, not {config.auth_mode!r}")
    for key, level in config.policy.items():
        if level not in ("silent", "notify", "ask"):
            raise ValueError(f"policy {key!r}: {level!r} must be silent, notify or ask")
    return config


def secret(name: str) -> str | None:
    """From the environment, or else from backend/.env."""
    found = os.environ.get(name)
    if not found and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                found = value.strip().strip("'\"")
    return found or None
