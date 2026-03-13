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
    sub.add_parser("run")
    sub.add_parser("restart")
    sub.add_parser("stop")
    sub.add_parser("unpair")
    sub.add_parser("status")
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

    if not config_store.exists():
        settings = run_setup_wizard(config_store.path)
        config_store.save(settings)
        print(f"Saved config to {config_store.path}")
    else:
        settings = config_store.load()

    settings.ensure_directories()
    settings.configure_logging()
    store = StateStore(settings.state_file)
    state = store.load()

    if args.command == "status":
        pid = read_pid(settings.pid_file)
        print(
            json.dumps(
                {
                    "daemon": {
                        "pid": pid,
                        "running": is_process_running(pid),
                        "pid_file": str(settings.pid_file),
                        "log_file": str(settings.log_file),
                    },
                    "pairing": asdict(state.pairing),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    if args.command == "unpair":
        clear_pairing(state)
        code = issue_pair_code(state, settings.pair_code_length, settings.pair_code_ttl_seconds)
        store.save(state)
        print("Bridge unpaired.")
        print(f"New pairing code: {code}")
        return

    if args.command == "stop":
        stopped = stop_process(settings.pid_file)
        print("Stopped teledex." if stopped else "teledex is not running.")
        return

    if args.command == "restart":
        stop_process(settings.pid_file)
        _ensure_pair_code(settings, state, store)
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
        _ensure_pair_code(settings, state, store)
        pid = start_background_process(settings.log_file)
        print(f"teledex started in background (pid {pid}).")
        print(f"Logs: {settings.log_file}")
        return

    from .telegram_bridge import TelegramBridge

    write_pid(settings.pid_file, os.getpid())
    _install_signal_cleanup(settings.pid_file)
    bridge = TelegramBridge(settings=settings, store=store, state=state)
    try:
        asyncio.run(bridge.run())
    finally:
        remove_pid_file(settings.pid_file)


def _ensure_pair_code(settings, state, store) -> None:
    if not state.pairing.is_paired() and not state.pairing.code_valid():
        code = issue_pair_code(state, settings.pair_code_length, settings.pair_code_ttl_seconds)
        store.save(state)
        print(f"Pairing code: {code}")
        print("Send this code to the Telegram bot from your private chat.")


def _install_signal_cleanup(pid_file) -> None:
    def _cleanup(_signum, _frame):
        remove_pid_file(pid_file)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)


if __name__ == "__main__":
    main()
