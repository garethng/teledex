"""Interactive TUI for `teledex config`.

Pure stdlib — no extra dependencies.
"""
from __future__ import annotations

import os
import shutil
from getpass import getpass
from pathlib import Path
from typing import Callable

from .config import (
    DEFAULT_MEMORY_FILE,
    DEFAULT_SESSION_STORAGE_DIR,
    DEFAULT_STATE_DIR,
    AgentType,
    BotConfig,
    ConfigStore,
    Settings,
)


# ── low-level prompt helpers ──────────────────────────────────────────────────

def _hr(char: str = "─", width: int = 60) -> None:
    print(char * width)


def _header(title: str) -> None:
    print()
    _hr()
    print(f"  {title}")
    _hr()


def _prompt(label: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default is not None else ""
    text = f"  {label}{suffix}: "
    try:
        value = getpass(text) if secret else input(text)
    except (EOFError, KeyboardInterrupt):
        print()
        raise
    value = value.strip()
    return value if value else (default or "")


def _choose(label: str, options: list[str], default: str | None = None) -> str:
    """Numbered menu. Returns the selected option string."""
    print(f"  {label}")
    for i, opt in enumerate(options, 1):
        marker = " ◀" if opt == default else ""
        print(f"    {i}. {opt}{marker}")
    while True:
        raw = _prompt("Select", default=default)
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        if raw in options:
            return raw
        if raw == default:
            return raw
        print("  Invalid choice, try again.")


def _confirm(label: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    raw = _prompt(f"{label} [{suffix}]").lower()
    if not raw:
        return default
    return raw.startswith("y")


def _path_prompt(label: str, default: Path) -> Path:
    raw = _prompt(label, str(default))
    return Path(raw).expanduser().resolve()


# ── bot display helpers ───────────────────────────────────────────────────────

def _bot_summary(bot: BotConfig) -> str:
    token_hint = bot.telegram_bot_token[:8] + "…" if bot.telegram_bot_token else "(none)"
    model_hint = f"  model: {bot.model}" if bot.model else ""
    return (
        f"  name:       {bot.name}\n"
        f"  type:       {bot.agent_type}\n"
        f"  token:      {token_hint}\n"
        f"  cmd:        {bot.agent_cmd}\n"
        f"  workspace:  {bot.workspace_root}\n"
        f"  memory:     {bot.memory_file}"
        + (f"\n{model_hint}" if model_hint else "")
    )


def _global_summary(s: Settings) -> str:
    return (
        f"  workspace:    {s.workspace_root}\n"
        f"  memory:       {s.memory_file}\n"
        f"  sessions dir: {s.session_storage_dir}\n"
        f"  log file:     {s.log_file}\n"
        f"  log level:    {s.log_level}\n"
        f"  transcriber:  {s.transcriber_backend} ({s.whisper_model})\n"
        f"  poll interval:{s.poll_interval_seconds}s\n"
        f"  pair TTL:     {s.pair_code_ttl_seconds}s  "
        f"max attempts: {s.pair_max_attempts}  "
        f"cooldown: {s.pair_cooldown_seconds}s"
    )


# ── bot add / edit wizard ─────────────────────────────────────────────────────

def _edit_bot(existing: BotConfig | None, settings: Settings, taken_names: set[str]) -> BotConfig | None:
    """Interactively create or edit a BotConfig. Returns None if aborted."""
    is_new = existing is None
    _header("Add New Bot" if is_new else f"Edit Bot: {existing.name}")  # type: ignore[union-attr]

    defaults = existing or BotConfig.defaults("new-bot")

    # Name
    while True:
        name = _prompt("Bot name (unique identifier)", defaults.name)
        if not name:
            print("  Name is required.")
            continue
        if name in taken_names:
            print(f"  Name '{name}' is already taken.")
            continue
        break

    # Token
    token = _prompt("Telegram bot token", secret=True) or defaults.telegram_bot_token
    if not token:
        print("  Token is required.")
        if not _confirm("Continue without token?", default=False):
            return None

    # Agent type
    agent_type_str = _choose("Agent type", ["codex", "opencode"], default=defaults.agent_type)
    agent_type: AgentType = "opencode" if agent_type_str == "opencode" else "codex"

    # Agent command
    default_cmd = "opencode" if agent_type == "opencode" else (settings.codex_cmd or "codex")
    agent_cmd = _prompt("Agent command", default_cmd) or default_cmd

    # Workspace
    workspace_root = _path_prompt("Workspace root", defaults.workspace_root)

    # Memory file
    memory_file = _path_prompt("Memory file", defaults.memory_file)

    # Session storage
    default_session_dir = DEFAULT_SESSION_STORAGE_DIR / name
    session_storage_dir = _path_prompt("Session storage dir", default_session_dir)

    # State file
    default_state_file = DEFAULT_STATE_DIR / f"{name}.json"
    state_file = _path_prompt("State file", default_state_file)

    # Model (opencode only)
    model: str | None = defaults.model if is_new else defaults.model
    if agent_type == "opencode":
        model_input = _prompt("Default model (provider/model, blank = opencode default)", defaults.model or "")
        model = model_input or None
    else:
        model = None

    bot = BotConfig(
        name=name,
        telegram_bot_token=token,
        agent_type=agent_type,
        workspace_root=workspace_root,
        memory_file=memory_file,
        agent_cmd=agent_cmd,
        session_storage_dir=session_storage_dir,
        state_file=state_file,
        model=model,
    )
    print()
    print("  Bot configuration:")
    print(_bot_summary(bot))
    if not _confirm("Save this bot?", default=True):
        return None
    return bot


# ── global settings editor ────────────────────────────────────────────────────

def _edit_global(settings: Settings) -> Settings:
    _header("Global Settings")
    print(_global_summary(settings))
    print()

    workspace_root = _path_prompt("Default workspace root", settings.workspace_root)
    memory_file = _path_prompt("Global memory file", settings.memory_file)
    session_storage_dir = _path_prompt("Session storage dir", settings.session_storage_dir)
    log_file = _path_prompt("Log file", settings.log_file)
    log_level = _choose(
        "Log level",
        ["DEBUG", "INFO", "WARNING", "ERROR"],
        default=settings.log_level,
    )
    transcriber_backend = _choose(
        "Voice transcription backend",
        ["faster-whisper", "disabled"],
        default=settings.transcriber_backend,
    )
    whisper_model = _prompt("Whisper model", settings.whisper_model)
    poll_interval_raw = _prompt("Poll interval (seconds)", str(settings.poll_interval_seconds))
    try:
        poll_interval = float(poll_interval_raw)
    except ValueError:
        poll_interval = settings.poll_interval_seconds

    pair_ttl_raw = _prompt("Pairing code TTL (seconds)", str(settings.pair_code_ttl_seconds))
    try:
        pair_ttl = int(pair_ttl_raw)
    except ValueError:
        pair_ttl = settings.pair_code_ttl_seconds

    pair_max_raw = _prompt("Max pairing attempts", str(settings.pair_max_attempts))
    try:
        pair_max = int(pair_max_raw)
    except ValueError:
        pair_max = settings.pair_max_attempts

    pair_cooldown_raw = _prompt("Pairing cooldown (seconds)", str(settings.pair_cooldown_seconds))
    try:
        pair_cooldown = int(pair_cooldown_raw)
    except ValueError:
        pair_cooldown = settings.pair_cooldown_seconds

    # Build new settings preserving bots and unchanged fields
    from dataclasses import replace  # type: ignore[attr-defined]
    # Settings uses slots=True so we reconstruct manually
    return Settings(
        bots=settings.bots,
        codex_cmd=settings.codex_cmd,
        workspace_root=workspace_root,
        memory_file=memory_file,
        session_storage_dir=session_storage_dir,
        pid_file=settings.pid_file,
        log_file=log_file,
        pair_code_ttl_seconds=pair_ttl,
        pair_code_length=settings.pair_code_length,
        pair_max_attempts=pair_max,
        pair_cooldown_seconds=pair_cooldown,
        transcriber_backend=transcriber_backend,
        whisper_model=whisper_model,
        poll_interval_seconds=poll_interval,
        log_level=log_level,
    )


# ── main TUI loop ─────────────────────────────────────────────────────────────

def run_config_tui(config_store: ConfigStore) -> None:
    """Main entry point for `teledex config`."""
    # Load or start fresh
    if config_store.exists():
        try:
            settings = config_store.load()
        except Exception as exc:
            print(f"Warning: could not load existing config ({exc}). Starting fresh.")
            settings = Settings.defaults()
    else:
        settings = Settings.defaults()

    dirty = False  # track unsaved changes

    while True:
        _header(f"teledex config  —  {config_store.path}")
        print(f"  Bots configured: {len(settings.bots)}")
        for i, bot in enumerate(settings.bots, 1):
            model_hint = f"  [{bot.model}]" if bot.model else ""
            print(f"    {i}. {bot.name}  ({bot.agent_type}){model_hint}")
        if dirty:
            print("\n  * Unsaved changes")
        print()

        action = _choose(
            "Action",
            [
                "Add bot",
                "Edit bot",
                "Remove bot",
                "Edit global settings",
                "Show full config",
                "Save & exit",
                "Exit without saving",
            ],
            default="Save & exit",
        )

        if action == "Add bot":
            taken = {b.name for b in settings.bots}
            bot = _edit_bot(None, settings, taken)
            if bot:
                settings.bots.append(bot)
                _save(config_store, settings)
                dirty = False
                print(f"  ✓ Bot '{bot.name}' saved.")
                _issue_and_show_pair_code(bot, settings)

        elif action == "Edit bot":
            if not settings.bots:
                print("  No bots configured yet.")
                continue
            _header("Edit Bot")
            for i, bot in enumerate(settings.bots, 1):
                print(f"    {i}. {bot.name}  ({bot.agent_type})")
            raw = _prompt("Select bot number (or name)")
            target_bot, target_idx = _find_bot(settings.bots, raw)
            if target_bot is None:
                print("  Bot not found.")
                continue
            taken = {b.name for j, b in enumerate(settings.bots) if j != target_idx}
            updated = _edit_bot(target_bot, settings, taken)
            if updated:
                settings.bots[target_idx] = updated
                dirty = True
                print(f"  ✓ Bot '{updated.name}' updated.")

        elif action == "Remove bot":
            if not settings.bots:
                print("  No bots configured yet.")
                continue
            _header("Remove Bot")
            for i, bot in enumerate(settings.bots, 1):
                print(f"    {i}. {bot.name}  ({bot.agent_type})")
            raw = _prompt("Select bot to remove (number or name)")
            target_bot, target_idx = _find_bot(settings.bots, raw)
            if target_bot is None:
                print("  Bot not found.")
                continue
            if _confirm(f"Remove bot '{target_bot.name}'?", default=False):
                settings.bots.pop(target_idx)
                dirty = True
                print(f"  ✓ Bot '{target_bot.name}' removed.")

        elif action == "Edit global settings":
            settings = _edit_global(settings)
            dirty = True
            print("  ✓ Global settings updated.")

        elif action == "Show full config":
            _header("Current Configuration")
            print("  Global:")
            print(_global_summary(settings))
            for i, bot in enumerate(settings.bots, 1):
                print(f"\n  Bot {i}:")
                print(_bot_summary(bot))

        elif action == "Save & exit":
            if not settings.bots:
                print("  At least one bot is required before saving.")
                if not _confirm("Exit without saving?", default=False):
                    continue
                return
            _save(config_store, settings)
            dirty = False
            print(f"  ✓ Saved to {config_store.path}")
            return

        elif action == "Exit without saving":
            if dirty and not _confirm("Discard unsaved changes?", default=False):
                continue
            return


def _find_bot(bots: list[BotConfig], raw: str) -> tuple[BotConfig | None, int]:
    """Return (bot, index) by number or name, or (None, -1)."""
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(bots):
            return bots[idx], idx
    for i, b in enumerate(bots):
        if b.name == raw:
            return b, i
    return None, -1


def _save(config_store: ConfigStore, settings: Settings) -> None:
    settings.ensure_directories()
    config_store.save(settings)


def _issue_and_show_pair_code(bot: BotConfig, settings: Settings) -> None:
    """Generate a pairing code for a newly added bot and print it prominently."""
    from .state import BridgeState, StateStore, issue_pair_code

    bot.ensure_directories()
    store = StateStore(bot.state_file)
    state = store.load()

    if state.pairing.is_paired():
        print(f"  Bot '{bot.name}' is already paired (chat id: {state.pairing.authorized_chat_id}).")
        return

    code = issue_pair_code(state, settings.pair_code_length, settings.pair_code_ttl_seconds)
    store.save(state)

    ttl_min = settings.pair_code_ttl_seconds // 60
    _hr("═")
    print(f"  Pairing code for bot '{bot.name}':")
    print()
    print(f"      {code}")
    print()
    print(f"  Send this code to the Telegram bot from your private chat.")
    print(f"  Expires in {ttl_min} minutes.")
    _hr("═")
