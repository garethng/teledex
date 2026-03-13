from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from getpass import getpass
from pathlib import Path


DEFAULT_CONFIG_FILE = Path("~/.teledex/config.json").expanduser().resolve()
DEFAULT_MEMORY_FILE = Path("~/.teledex/Memory.md").expanduser().resolve()
DEFAULT_STATE_FILE = Path("~/.teledex/state.json").expanduser().resolve()
DEFAULT_SESSION_STORAGE_DIR = Path("~/.teledex/sessions").expanduser().resolve()
DEFAULT_PID_FILE = Path("~/.teledex/teledex.pid").expanduser().resolve()
DEFAULT_LOG_FILE = Path("~/.teledex/teledex.log").expanduser().resolve()


@dataclass(slots=True)
class Settings:
    telegram_bot_token: str
    codex_cmd: str
    workspace_root: Path
    memory_file: Path
    state_file: Path
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
            telegram_bot_token="",
            codex_cmd="codex",
            workspace_root=Path(os.getcwd()).expanduser().resolve(),
            memory_file=DEFAULT_MEMORY_FILE,
            state_file=DEFAULT_STATE_FILE,
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
        merged = {
            **asdict(defaults),
            **payload,
        }
        return cls(
            telegram_bot_token=str(merged["telegram_bot_token"]).strip(),
            codex_cmd=str(merged["codex_cmd"]).strip() or defaults.codex_cmd,
            workspace_root=Path(merged["workspace_root"]).expanduser().resolve(),
            memory_file=Path(merged["memory_file"]).expanduser().resolve(),
            state_file=Path(merged["state_file"]).expanduser().resolve(),
            session_storage_dir=Path(merged["session_storage_dir"]).expanduser().resolve(),
            pid_file=Path(merged["pid_file"]).expanduser().resolve(),
            log_file=Path(merged["log_file"]).expanduser().resolve(),
            pair_code_ttl_seconds=int(merged["pair_code_ttl_seconds"]),
            pair_code_length=int(merged["pair_code_length"]),
            pair_max_attempts=int(merged["pair_max_attempts"]),
            pair_cooldown_seconds=int(merged["pair_cooldown_seconds"]),
            transcriber_backend=str(merged["transcriber_backend"]).strip() or defaults.transcriber_backend,
            whisper_model=str(merged["whisper_model"]).strip() or defaults.whisper_model,
            poll_interval_seconds=float(merged["poll_interval_seconds"]),
            log_level=str(merged["log_level"]).upper().strip() or defaults.log_level,
        )

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["workspace_root"] = str(self.workspace_root)
        payload["memory_file"] = str(self.memory_file)
        payload["state_file"] = str(self.state_file)
        payload["session_storage_dir"] = str(self.session_storage_dir)
        payload["pid_file"] = str(self.pid_file)
        payload["log_file"] = str(self.log_file)
        return payload

    def ensure_directories(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.session_storage_dir.mkdir(parents=True, exist_ok=True)
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

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
    print("teledex initial setup")
    print(f"Config file: {config_path.expanduser().resolve()}")
    token = _prompt_text("Telegram bot token", current.telegram_bot_token or None, secret=True)
    codex_cmd = _prompt_text("Codex command", current.codex_cmd)
    workspace_root = Path(_prompt_text("Workspace root", str(current.workspace_root))).expanduser().resolve()
    memory_file = Path(
        _prompt_text("Global memory file", str(current.memory_file))
    ).expanduser().resolve()
    state_file = Path(_prompt_text("State file", str(current.state_file))).expanduser().resolve()
    session_storage_dir = Path(
        _prompt_text("Session storage directory", str(current.session_storage_dir))
    ).expanduser().resolve()
    pid_file = Path(_prompt_text("PID file", str(current.pid_file))).expanduser().resolve()
    log_file = Path(_prompt_text("Log file", str(current.log_file))).expanduser().resolve()
    transcriber_backend = _prompt_choice(
        "Voice transcription backend",
        ["faster-whisper", "disabled"],
        current.transcriber_backend if current.transcriber_backend in {"faster-whisper", "disabled"} else "faster-whisper",
    )
    whisper_model = _prompt_text("Whisper model", current.whisper_model)
    settings = Settings(
        telegram_bot_token=token,
        codex_cmd=codex_cmd,
        workspace_root=workspace_root,
        memory_file=memory_file,
        state_file=state_file,
        session_storage_dir=session_storage_dir,
        pid_file=pid_file,
        log_file=log_file,
        pair_code_ttl_seconds=current.pair_code_ttl_seconds,
        pair_code_length=current.pair_code_length,
        pair_max_attempts=current.pair_max_attempts,
        pair_cooldown_seconds=current.pair_cooldown_seconds,
        transcriber_backend=transcriber_backend,
        whisper_model=whisper_model,
        poll_interval_seconds=current.poll_interval_seconds,
        log_level=current.log_level,
    )
    if not settings.telegram_bot_token:
        raise ValueError("Telegram bot token is required")
    return settings
