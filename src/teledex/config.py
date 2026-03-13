from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from getpass import getpass
from pathlib import Path
from typing import Literal


DEFAULT_CONFIG_FILE = Path("~/.teledex/config.json").expanduser().resolve()
DEFAULT_MEMORY_FILE = Path("~/.teledex/Memory.md").expanduser().resolve()
DEFAULT_AGENTS_DIR = Path("~/.teledex/agents").expanduser().resolve()
DEFAULT_SESSION_STORAGE_DIR = Path("~/.teledex/sessions").expanduser().resolve()
DEFAULT_PID_FILE = Path("~/.teledex/teledex.pid").expanduser().resolve()
DEFAULT_LOG_FILE = Path("~/.teledex/teledex.log").expanduser().resolve()
DEFAULT_STATE_DIR = Path("~/.teledex/state").expanduser().resolve()

# Legacy single-bot state file path
DEFAULT_STATE_FILE = Path("~/.teledex/state.json").expanduser().resolve()

AgentType = Literal["codex", "opencode"]


@dataclass(slots=True)
class BotConfig:
    """Configuration for a single Telegram bot / agent pair."""

    name: str
    telegram_bot_token: str
    agent_type: AgentType
    workspace_root: Path
    memory_file: Path
    agent_cmd: str  # e.g. "codex" or "opencode"

    # Per-bot overrides (fall back to global Settings values if not set)
    session_storage_dir: Path
    state_file: Path
    log_file: Path

    # opencode-only: optional model override (e.g. "openrouter/anthropic/claude-opus-4-6")
    # None means use opencode's default / configured model
    model: str | None

    @classmethod
    def defaults(cls, name: str = "default") -> "BotConfig":
        return cls(
            name=name,
            telegram_bot_token="",
            agent_type="codex",
            workspace_root=Path(os.getcwd()).expanduser().resolve(),
            memory_file=DEFAULT_AGENTS_DIR / name / "MEMORY.md",
            agent_cmd="codex",
            session_storage_dir=DEFAULT_SESSION_STORAGE_DIR / name,
            state_file=DEFAULT_STATE_DIR / f"{name}.json",
            log_file=DEFAULT_AGENTS_DIR / name / "bot.log",
            model=None,
        )

    @classmethod
    def from_dict(cls, payload: dict, name: str = "default", global_settings: "Settings | None" = None) -> "BotConfig":
        defaults = cls.defaults(name)
        g = global_settings

        def _path(key: str, default: Path) -> Path:
            val = payload.get(key)
            if val:
                return Path(val).expanduser().resolve()
            return default

        agent_type_raw = str(payload.get("agent_type", "codex")).strip().lower()
        agent_type: AgentType = "opencode" if agent_type_raw == "opencode" else "codex"

        # agent_cmd defaults: use agent_type name unless overridden
        default_agent_cmd = "opencode" if agent_type == "opencode" else (g.codex_cmd if g else "codex")

        model_raw = payload.get("model")
        model = str(model_raw).strip() or None if model_raw else None

        return cls(
            name=name,
            telegram_bot_token=str(payload.get("telegram_bot_token", "")).strip(),
            agent_type=agent_type,
            workspace_root=_path("workspace_root", defaults.workspace_root),
            memory_file=_path("memory_file", defaults.memory_file),
            agent_cmd=str(payload.get("agent_cmd", default_agent_cmd)).strip() or default_agent_cmd,
            session_storage_dir=_path(
                "session_storage_dir",
                (g.session_storage_dir / name) if g else defaults.session_storage_dir,
            ),
            state_file=_path("state_file", defaults.state_file),
            log_file=_path("log_file", defaults.log_file),
            model=model,
        )

    def to_dict(self) -> dict:
        d: dict = {
            "name": self.name,
            "telegram_bot_token": self.telegram_bot_token,
            "agent_type": self.agent_type,
            "workspace_root": str(self.workspace_root),
            "memory_file": str(self.memory_file),
            "agent_cmd": self.agent_cmd,
            "session_storage_dir": str(self.session_storage_dir),
            "state_file": str(self.state_file),
            "log_file": str(self.log_file),
        }
        if self.model is not None:
            d["model"] = self.model
        return d

    def ensure_directories(self) -> None:
        self.session_storage_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)


@dataclass(slots=True)
class Settings:
    """Global settings shared across all bots."""

    bots: list[BotConfig]

    # Shared defaults (used when a bot doesn't override)
    codex_cmd: str
    workspace_root: Path
    memory_file: Path
    session_storage_dir: Path
    pid_file: Path
    log_file: Path

    pair_code_ttl_seconds: int
    pair_code_length: int
    pair_max_attempts: int
    pair_cooldown_seconds: int
    transcriber_backend: str
    whisper_model: str
    poll_interval_seconds: float
    log_level: str

    @classmethod
    def defaults(cls) -> "Settings":
        return cls(
            bots=[],
            codex_cmd="codex",
            workspace_root=Path(os.getcwd()).expanduser().resolve(),
            memory_file=DEFAULT_MEMORY_FILE,
            session_storage_dir=DEFAULT_SESSION_STORAGE_DIR,
            pid_file=DEFAULT_PID_FILE,
            log_file=DEFAULT_LOG_FILE,
            pair_code_ttl_seconds=600,
            pair_code_length=6,
            pair_max_attempts=5,
            pair_cooldown_seconds=60,
            transcriber_backend="faster-whisper",
            whisper_model="base",
            poll_interval_seconds=1.0,
            log_level="INFO",
        )

    @classmethod
    def from_dict(cls, payload: dict) -> "Settings":
        defaults = cls.defaults()

        def _str(key: str, default: str) -> str:
            return str(payload.get(key, default)).strip() or default

        def _int(key: str, default: int) -> int:
            return int(payload.get(key, default))

        def _float(key: str, default: float) -> float:
            return float(payload.get(key, default))

        def _path(key: str, default: Path) -> Path:
            val = payload.get(key)
            if val:
                return Path(val).expanduser().resolve()
            return default

        # Build partial global settings first (needed for bot defaults)
        global_partial = cls(
            bots=[],
            codex_cmd=_str("codex_cmd", defaults.codex_cmd),
            workspace_root=_path("workspace_root", defaults.workspace_root),
            memory_file=_path("memory_file", defaults.memory_file),
            session_storage_dir=_path("session_storage_dir", defaults.session_storage_dir),
            pid_file=_path("pid_file", defaults.pid_file),
            log_file=_path("log_file", defaults.log_file),
            pair_code_ttl_seconds=_int("pair_code_ttl_seconds", defaults.pair_code_ttl_seconds),
            pair_code_length=_int("pair_code_length", defaults.pair_code_length),
            pair_max_attempts=_int("pair_max_attempts", defaults.pair_max_attempts),
            pair_cooldown_seconds=_int("pair_cooldown_seconds", defaults.pair_cooldown_seconds),
            transcriber_backend=_str("transcriber_backend", defaults.transcriber_backend),
            whisper_model=_str("whisper_model", defaults.whisper_model),
            poll_interval_seconds=_float("poll_interval_seconds", defaults.poll_interval_seconds),
            log_level=_str("log_level", defaults.log_level).upper(),
        )

        # Parse bots list
        bots: list[BotConfig] = []
        bots_raw = payload.get("bots")

        if bots_raw and isinstance(bots_raw, list):
            for i, bot_payload in enumerate(bots_raw):
                if not isinstance(bot_payload, dict):
                    continue
                name = str(bot_payload.get("name", f"bot{i}")).strip() or f"bot{i}"
                bots.append(BotConfig.from_dict(bot_payload, name=name, global_settings=global_partial))
        elif payload.get("telegram_bot_token"):
            # Legacy single-bot format: promote to a single BotConfig
            bots.append(BotConfig.from_dict(
                {
                    "telegram_bot_token": payload["telegram_bot_token"],
                    "agent_type": "codex",
                    "agent_cmd": payload.get("codex_cmd", "codex"),
                    "workspace_root": payload.get("workspace_root"),
                    "memory_file": payload.get("memory_file"),
                    "session_storage_dir": payload.get("session_storage_dir"),
                    "state_file": payload.get("state_file"),
                },
                name="default",
                global_settings=global_partial,
            ))

        global_partial.bots = bots
        return global_partial

    def to_dict(self) -> dict:
        return {
            "bots": [b.to_dict() for b in self.bots],
            "codex_cmd": self.codex_cmd,
            "workspace_root": str(self.workspace_root),
            "memory_file": str(self.memory_file),
            "session_storage_dir": str(self.session_storage_dir),
            "pid_file": str(self.pid_file),
            "log_file": str(self.log_file),
            "pair_code_ttl_seconds": self.pair_code_ttl_seconds,
            "pair_code_length": self.pair_code_length,
            "pair_max_attempts": self.pair_max_attempts,
            "pair_cooldown_seconds": self.pair_cooldown_seconds,
            "transcriber_backend": self.transcriber_backend,
            "whisper_model": self.whisper_model,
            "poll_interval_seconds": self.poll_interval_seconds,
            "log_level": self.log_level,
        }

    def ensure_directories(self) -> None:
        self.session_storage_dir.mkdir(parents=True, exist_ok=True)
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
        DEFAULT_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        for bot in self.bots:
            bot.ensure_directories()

    def configure_logging(self) -> None:
        logging.basicConfig(
            level=getattr(logging, self.log_level, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )


class ConfigStore:
    def __init__(self, path: Path = DEFAULT_CONFIG_FILE):
        self.path = path.expanduser().resolve()

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> Settings:
        if not self.path.exists():
            raise FileNotFoundError(f"Missing config file: {self.path}")
        raw = self.path.read_text(encoding="utf-8").strip()
        if not raw:
            raise ValueError(f"Config file is empty: {self.path}")
        payload = json.loads(raw)
        return Settings.from_dict(payload)

    def save(self, settings: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(settings.to_dict(), indent=2, sort_keys=True), encoding="utf-8")


def _prompt_text(label: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    prompt = f"{label}{suffix}: "
    value = getpass(prompt) if secret else input(prompt)
    value = value.strip()
    if value:
        return value
    return default or ""


def _prompt_choice(label: str, options: list[str], default: str) -> str:
    print(label)
    for index, option in enumerate(options, start=1):
        marker = " (default)" if option == default else ""
        print(f"  {index}. {option}{marker}")
    raw = input("Select an option: ").strip()
    if not raw:
        return default
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return options[idx]
    if raw in options:
        return raw
    print(f"Invalid choice, using default: {default}")
    return default


def run_setup_wizard(config_path: Path = DEFAULT_CONFIG_FILE, existing: Settings | None = None) -> Settings:
    current = existing or Settings.defaults()
    print("teledex setup")
    print(f"Config file: {config_path.expanduser().resolve()}")
    print()

    # Global settings
    workspace_root = Path(_prompt_text("Default workspace root", str(current.workspace_root))).expanduser().resolve()
    memory_file = Path(_prompt_text("Global memory file", str(current.memory_file))).expanduser().resolve()
    transcriber_backend = _prompt_choice(
        "Voice transcription backend",
        ["faster-whisper", "disabled"],
        current.transcriber_backend if current.transcriber_backend in {"faster-whisper", "disabled"} else "faster-whisper",
    )
    whisper_model = _prompt_text("Whisper model", current.whisper_model)

    # Bots
    bots: list[BotConfig] = list(current.bots)
    print()
    print(f"Currently configured bots: {len(bots)}")
    while True:
        action = _prompt_choice(
            "Bot configuration",
            ["Add a bot", "Done"],
            "Done",
        )
        if action == "Done":
            break
        _add_bot_wizard(bots, workspace_root, memory_file)

    if not bots:
        raise ValueError("At least one bot must be configured")

    settings = Settings(
        bots=bots,
        codex_cmd=current.codex_cmd,
        workspace_root=workspace_root,
        memory_file=memory_file,
        session_storage_dir=current.session_storage_dir,
        pid_file=current.pid_file,
        log_file=current.log_file,
        pair_code_ttl_seconds=current.pair_code_ttl_seconds,
        pair_code_length=current.pair_code_length,
        pair_max_attempts=current.pair_max_attempts,
        pair_cooldown_seconds=current.pair_cooldown_seconds,
        transcriber_backend=transcriber_backend,
        whisper_model=whisper_model,
        poll_interval_seconds=current.poll_interval_seconds,
        log_level=current.log_level,
    )
    return settings


def _add_bot_wizard(bots: list[BotConfig], workspace_root: Path, memory_file: Path) -> None:
    name = _prompt_text("Bot name (unique identifier)", f"bot{len(bots) + 1}")
    token = _prompt_text("Telegram bot token", secret=True)
    if not token:
        print("Token is required, skipping.")
        return
    agent_type_str = _prompt_choice(
        "Agent type",
        ["codex", "opencode"],
        "codex",
    )
    agent_type: AgentType = "opencode" if agent_type_str == "opencode" else "codex"
    default_cmd = "opencode" if agent_type == "opencode" else "codex"
    agent_cmd = _prompt_text("Agent command", default_cmd)
    bot_workspace = Path(
        _prompt_text("Workspace root for this bot", str(workspace_root))
    ).expanduser().resolve()
    bot_memory = Path(
        _prompt_text("Memory file for this bot", str(memory_file))
    ).expanduser().resolve()
    session_dir = Path(
        _prompt_text("Session storage dir", str(DEFAULT_SESSION_STORAGE_DIR / name))
    ).expanduser().resolve()
    state_file = Path(
        _prompt_text("State file", str(DEFAULT_STATE_DIR / f"{name}.json"))
    ).expanduser().resolve()

    default_log_file = DEFAULT_AGENTS_DIR / name / "bot.log"
    
    bots.append(BotConfig(
        name=name,
        telegram_bot_token=token,
        agent_type=agent_type,
        workspace_root=bot_workspace,
        memory_file=bot_memory,
        agent_cmd=agent_cmd,
        session_storage_dir=session_dir,
        state_file=state_file,
        log_file=default_log_file,
        model=None,
    ))
    print(f"Bot '{name}' ({agent_type}) added.")
