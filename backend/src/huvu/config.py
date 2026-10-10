"""Configuration: one TOML file, with secrets from the environment.

    data_dir = "data"
    timezone = "Asia/Kolkata"  # the house's: every "local" time, reminders, the dream
    public_url = "http://localhost:3001"
    port = 3001
    cors_origins = ["*"]
    web_dir = "../frontend/dist"   # the web client's build, served at /
    bundles_dir = "bundles"        # each native app's build, for live updates (bundles.py)

    [auth]
    mode = "on"              # or "off", for development on localhost
    dev_user = "you"         # with mode = "off", whose requests they are

    [hermes]
    url = "http://localhost:8642"

    [policy]
    "ha.turn_on" = "silent"  # source.action = silent | notify | ask

    [models]                 # the three tiers of model huvu runs on
    basic = "gpt-6-luna"     # plain house control, and trigger wakes
    pro = "gpt-6-sol"        # every other ask, and wakes that need more thought
    advanced = "gpt-6-astra" # wakes only, and only with the person's yes
    # A model alone runs on the profile's own login. To run a tier on another
    # Hermes provider, name it: anthropic is the Claude login, openai-codex
    # the ChatGPT one.
    # pro = { model = "claude-sonnet-5-5", provider = "anthropic" }

    [router]                 # one-offs on basic, the rest on pro (routing.py)
    enabled = true           # false runs every ask on pro
    effort = "none"          # the reasoning effort of an ask sent to basic
    escalate_at = 0.30       # Clef-flash's probability that an ask needs context, at which it runs on pro
    timeout = 1.0            # seconds Clef-flash may take; slower, and the ask runs on pro

    [dream]
    at = "04:00"             # each person's nightly dream, local time
    tier = "pro"

    [speech]
    voice = "en-US-AvaNeural"   # an Edge voice: uv run edge-tts --list-voices
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
GROQ_API_KEY turns on speech to text (hearing.py). CLOUDFLARE_ACCOUNT_ID
and CLOUDFLARE_AI_TOKEN (a token with Workers AI read) let the router ask
Clef-flash whether an ask needs the conversation so far (routing.py);
without them every ask runs on pro. HUVU_TIMEZONE, which
setup.sh writes, overrides `timezone`: an IANA name, Asia/Kolkata unless
told otherwise. Never the server's own zone, which is UTC in the container. Users, devices and each user's
profile secrets live in the database (see accounts.py), managed with
`huvu user ...`.
"""

from __future__ import annotations

import os
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .spec import Autonomy

# The repo this backend runs from: backend/src/huvu/config.py, three up.
REPO = Path(__file__).resolve().parents[3]
# The model behind each tier, on the ChatGPT login Hermes uses.
DEFAULT_MODELS = {"basic": "gpt-6-luna", "pro": "gpt-6-sol", "advanced": "gpt-6-astra"}


@dataclass(frozen=True)
class ModelChoice:
    """A tier's model, and the Hermes provider to run it on; None means the
    profile's own provider."""

    model: str
    provider: str | None = None

    def __str__(self) -> str:
        return self.model


def _choice(tier: str, raw: Any) -> ModelChoice:
    if isinstance(raw, str):
        return ModelChoice(raw)
    if isinstance(raw, dict) and isinstance(raw.get("model"), str):
        return ModelChoice(raw["model"], raw.get("provider"))
    raise ValueError(f"models.{tier} must be a model name or {{ model = ..., provider = ... }}")
# Secrets setup.sh keeps, for when the environment doesn't have them.
# HUVU_ENV_FILE moves it, as the container does, onto its data volume.
ENV_FILE = Path(os.environ.get("HUVU_ENV_FILE") or REPO / "backend" / ".env")


@dataclass
class SourceConfig:
    name: str
    driver: str
    options: dict[str, Any] = field(default_factory=dict)


DEFAULT_TIMEZONE = "Asia/Kolkata"


@dataclass
class Config:
    data_dir: Path = Path("data")
    # The house's timezone, an IANA name.
    timezone: str = DEFAULT_TIMEZONE
    public_url: str = "http://localhost:3001"
    host: str = "0.0.0.0"
    port: int = 3001
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    # The web client's build (frontend/dist), served at /. None serves no
    # client, as in development, where Vite serves it.
    web_dir: Path | None = None
    # Each native target's web build, one folder per target, for the apps'
    # live updates (bundles.py). None serves none, and the apps keep the
    # build their APK came with.
    bundles_dir: Path | None = None
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
    # huvu's voice (speech.py): an Edge voice, and how fast it speaks.
    speech_voice: str = "en-US-AvaNeural"
    speech_speed: float = 1.0
    # The model behind each tier (Tier in spec.py). A conversation's asks
    # run on the tier the router picks, pro when it can't; a trigger's
    # wakes on its own tier, basic by default.
    models: dict[str, ModelChoice] = field(default_factory=lambda: {t: ModelChoice(m) for t, m in DEFAULT_MODELS.items()})
    # Each person's nightly dream (dream.py): when, and on which tier.
    dream_at: str = "04:00"
    dream_tier: str = "pro"
    # Speech to text on Groq (hearing.py). No key, no hearing.
    groq_api_key: str | None = None
    stt_model: str = "whisper-large-v3"
    # The router (routing.py): one-off asks on basic, the rest on pro. Off
    # runs every ask on pro.
    router_enabled: bool = True
    # Clef-flash on Cloudflare Workers AI, which says whether an ask needs
    # the conversation so far. Without both, every ask runs on pro.
    cloudflare_account_id: str | None = None
    cloudflare_ai_token: str | None = None
    # Clef-flash's probability that an ask needs the conversation so far, at
    # which it runs on pro.
    router_escalate_at: float = 0.30
    # Seconds Clef-flash may take before the ask runs on pro without it.
    router_timeout: float = 1.0
    # The reasoning effort of an ask the router sends to basic, as Hermes
    # names it: none, minimal, low, medium, high.
    router_effort: str = "none"

    def model(self, tier: str) -> ModelChoice:
        return self.models.get(tier) or ModelChoice(DEFAULT_MODELS[tier])

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
    router = raw.get("router", {})
    base = path.parent if path.exists() else Path.cwd()
    fcm = secret("HUVU_FCM_CREDENTIALS")
    # HUVU_WEB_DIR wins, so the image names where it put the build.
    web = os.environ.get("HUVU_WEB_DIR") or raw.get("web_dir")
    bundles = os.environ.get("HUVU_BUNDLES_DIR") or raw.get("bundles_dir")
    timezone = secret("HUVU_TIMEZONE") or raw.get("timezone") or DEFAULT_TIMEZONE
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise ValueError(f"timezone {timezone!r} isn't an IANA timezone, such as Asia/Kolkata") from err
    config = Config(
        data_dir=(base / raw.get("data_dir", "data")).resolve(),
        timezone=timezone,
        # HUVU_PUBLIC_URL wins, so the container's address lives in its
        # compose file, next to the tunnel that makes it.
        public_url=(os.environ.get("HUVU_PUBLIC_URL") or raw.get("public_url", "http://localhost:3001")).rstrip("/"),
        host=raw.get("host", "0.0.0.0"),
        port=int(raw.get("port", 3001)),
        cors_origins=list(raw.get("cors_origins", ["*"])),
        web_dir=(base / web).resolve() if web else None,
        bundles_dir=(base / bundles).resolve() if bundles else None,
        auth_mode=auth.get("mode", "on"),
        dev_user=auth.get("dev_user"),
        mcp_token=secret("HUVU_MCP_TOKEN"),
        hermes_url=hermes.get("url", "http://localhost:8642"),
        hermes_api_key=secret("HERMES_API_KEY"),
        fcm_credentials=Path(fcm).expanduser() if fcm else None,
        policy=dict(raw.get("policy", {})),
        skills_dir=(base / raw["skills_dir"]).resolve() if raw.get("skills_dir") else None,
        hermes_skills_dir=(base / raw["hermes_skills_dir"]).resolve() if raw.get("hermes_skills_dir") else REPO / "hermes-skills",
        models={t: _choice(t, m) for t, m in {**DEFAULT_MODELS, **raw.get("models", {})}.items()},
        dream_at=str(dream.get("at", "04:00")),
        dream_tier=str(dream.get("tier", "pro")),
        speech_voice=speech.get("voice", "en-US-AvaNeural"),
        speech_speed=float(speech.get("speed", 1.0)),
        groq_api_key=secret("GROQ_API_KEY"),
        stt_model=speech.get("stt_model", "whisper-large-v3"),
        router_enabled=bool(router.get("enabled", True)),
        cloudflare_account_id=secret("CLOUDFLARE_ACCOUNT_ID"),
        cloudflare_ai_token=secret("CLOUDFLARE_AI_TOKEN"),
        router_escalate_at=float(router.get("escalate_at", 0.30)),
        router_timeout=float(router.get("timeout", 1.0)),
        router_effort=str(router.get("effort", "none")),
        sources=[SourceConfig(s["name"], s["driver"], dict(s.get("options", {}))) for s in raw.get("sources", [])],
    )
    for tier in config.models:
        if tier not in DEFAULT_MODELS:
            raise ValueError(f"models.{tier}: the tiers are {', '.join(DEFAULT_MODELS)}")
    if config.dream_tier not in DEFAULT_MODELS:
        raise ValueError(f"dream.tier must be one of {', '.join(DEFAULT_MODELS)}")
    if not 0 < config.router_escalate_at <= 1:
        raise ValueError(f"router.escalate_at must be above 0 and at most 1, not {config.router_escalate_at}")
    if config.auth_mode not in ("on", "off"):
        raise ValueError(f"auth.mode must be on or off, not {config.auth_mode!r}")
    for key, level in config.policy.items():
        if level not in ("silent", "notify", "ask"):
            raise ValueError(f"policy {key!r}: {level!r} must be silent, notify or ask")
    return config


def secret(name: str) -> str | None:
    """From the environment, or else from backend/.env (HUVU_ENV_FILE)."""
    found = os.environ.get(name)
    if not found and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                found = value.strip().strip("'\"")
    return found or None
