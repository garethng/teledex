from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
from dataclasses import asdict

from .config import ConfigStore, DEFAULT_CONFIG_FILE, run_setup_wizard
from .daemon import (
    is_process_running,
    read_pid,
    remove_pid_file,
    start_background_process,
    stop_process,
    write_pid,
)
from .state import StateStore, clear_pairing, issue_pair_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="teledex")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("_serve")
    sub.add_parser("init")
    sub.add_parser("config")
    sub.add_parser("run")
    sub.add_parser("restart")
    sub.add_parser("stop")
    sub.add_parser("status")

    pair_p = sub.add_parser("pair", help="Generate a pairing code for a bot")
    pair_p.add_argument("bot", nargs="?", default=None, metavar="BOT", help="Bot name (default: all unpaired bots)")

    unpair_p = sub.add_parser("unpair", help="Unpair a bot and generate a new pairing code")
    unpair_p.add_argument("bot", nargs="?", default=None, metavar="BOT", help="Bot name (default: all bots)")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config_store = ConfigStore(DEFAULT_CONFIG_FILE)

    if args.command == "init":
        existing = config_store.load() if config_store.exists() else None
        settings = run_setup_wizard(config_store.path, existing=existing)
        config_store.save(settings)
        settings.ensure_directories()
        print(f"Saved config to {config_store.path}")
        return

    if args.command == "config":
        from .config_tui import run_config_tui
        run_config_tui(config_store)
        return

    if not config_store.exists():
        settings = run_setup_wizard(config_store.path)
        config_store.save(settings)
        print(f"Saved config to {config_store.path}")
    else:
        settings = config_store.load()

    settings.ensure_directories()
    settings.configure_logging()

    if args.command == "status":
        pid = read_pid(settings.pid_file)
        bot_states = {}
        for bot_cfg in settings.bots:
            store = StateStore(bot_cfg.state_file)
            state = store.load()
            bot_states[bot_cfg.name] = {
                "agent_type": bot_cfg.agent_type,
                "pairing": asdict(state.pairing),
            }
        print(
            json.dumps(
                {
                    "daemon": {
                        "pid": pid,
                        "running": is_process_running(pid),
                        "pid_file": str(settings.pid_file),
                        "log_file": str(settings.log_file),
                    },
                    "bots": bot_states,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    if args.command == "pair":
        bot_filter = args.bot
        if bot_filter is None:
            picked = _pick_bot(settings.bots, "Select bot to pair")
            if picked is _ABORT:
                return
            bot_filter = None if picked is _ALL else picked
        targets = _resolve_bots(settings.bots, bot_filter)
        if targets is None:
            print(f"No bot found with name '{bot_filter}'.")
            return
        for bot_cfg in targets:
            store = StateStore(bot_cfg.state_file)
            state = store.load()
            if state.pairing.is_paired():
                print(f"[{bot_cfg.name}] Already paired (chat id: {state.pairing.authorized_chat_id}). Use 'unpair' first.")
                continue
            code = issue_pair_code(state, settings.pair_code_length, settings.pair_code_ttl_seconds)
            store.save(state)
            _print_pair_code(bot_cfg.name, code, settings.pair_code_ttl_seconds)
        return

    if args.command == "unpair":
        bot_filter = args.bot
        if bot_filter is None:
            picked = _pick_bot(settings.bots, "Select bot to unpair")
            if picked is _ABORT:
                return
            bot_filter = None if picked is _ALL else picked
        targets = _resolve_bots(settings.bots, bot_filter)
        if targets is None:
            print(f"No bot found with name '{bot_filter}'.")
            return
        for bot_cfg in targets:
            store = StateStore(bot_cfg.state_file)
            state = store.load()
            clear_pairing(state)
            code = issue_pair_code(state, settings.pair_code_length, settings.pair_code_ttl_seconds)
            store.save(state)
            print(f"[{bot_cfg.name}] Unpaired.")
            _print_pair_code(bot_cfg.name, code, settings.pair_code_ttl_seconds)
        return

    if args.command == "stop":
        stopped = stop_process(settings.pid_file)
        print("Stopped teledex." if stopped else "teledex is not running.")
        return

    if args.command == "restart":
        stop_process(settings.pid_file)
        _ensure_pair_codes(settings)
        pid = start_background_process(settings.log_file)
        print(f"teledex restarted in background (pid {pid}).")
        print(f"Logs: {settings.log_file}")
        return

    if args.command == "run":
        pid = read_pid(settings.pid_file)
        if is_process_running(pid):
            print(f"teledex is already running (pid {pid}).")
            print(f"Logs: {settings.log_file}")
            return
        _ensure_pair_codes(settings)
        pid = start_background_process(settings.log_file)
        print(f"teledex started in background (pid {pid}).")
        print(f"Logs: {settings.log_file}")
        return

    # _serve: runs in the background process
    from .telegram_bridge import MultiAgentRunner

    write_pid(settings.pid_file, os.getpid())
    _install_signal_cleanup(settings.pid_file)
    runner = MultiAgentRunner(settings)
    try:
        asyncio.run(runner.run())
    finally:
        remove_pid_file(settings.pid_file)


_ALL = object()  # sentinel: user chose "All"
_ABORT = object()  # sentinel: invalid / cancelled


def _pick_bot(bots, prompt: str):
    """Interactive bot selector.

    Returns:
      bot name (str)  — specific bot selected
      _ALL            — user chose "All"
      _ABORT          — invalid input or no bots
    """
    if not bots:
        print("No bots configured.")
        return _ABORT
    print(f"{prompt}:")
    for i, b in enumerate(bots, 1):
        print(f"  {i}. {b.name}  ({b.agent_type})")
    print(f"  {len(bots) + 1}. All")
    raw = input("Select: ").strip()
    if raw.isdigit():
        idx = int(raw) - 1
        if idx == len(bots):
            return _ALL
        if 0 <= idx < len(bots):
            return bots[idx].name
    for b in bots:
        if b.name == raw:
            return raw
    print("Invalid selection.")
    return _ABORT


def _resolve_bots(bots, name_filter):
    """Return matching bots list, or None if name_filter was given but not found."""
    if name_filter is None:
        return bots
    matched = [b for b in bots if b.name == name_filter]
    return matched if matched else None


def _print_pair_code(bot_name: str, code: str, ttl_seconds: int) -> None:
    ttl_min = ttl_seconds // 60
    print(f"[{bot_name}] Pairing code: {code}  (expires in {ttl_min} min)")
    print(f"[{bot_name}] Send this code to the Telegram bot from your private chat.")


def _ensure_pair_codes(settings) -> None:
    for bot_cfg in settings.bots:
        store = StateStore(bot_cfg.state_file)
        state = store.load()
        if not state.pairing.is_paired() and not state.pairing.code_valid():
            code = issue_pair_code(state, settings.pair_code_length, settings.pair_code_ttl_seconds)
            store.save(state)
            print(f"[{bot_cfg.name}] Pairing code: {code}")
            print(f"[{bot_cfg.name}] Send this code to the Telegram bot from your private chat.")


def _install_signal_cleanup(pid_file) -> None:
    def _cleanup(_signum, _frame):
        remove_pid_file(pid_file)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)


if __name__ == "__main__":
    main()
