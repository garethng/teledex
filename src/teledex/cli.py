from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from .config import ConfigStore, DEFAULT_CONFIG_FILE, run_setup_wizard
from .state import StateStore, clear_pairing, issue_pair_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="teledex")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("run")
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
        print(json.dumps({"pairing": asdict(state.pairing)}, indent=2, sort_keys=True))
        return

    if args.command == "unpair":
        clear_pairing(state)
        code = issue_pair_code(state, settings.pair_code_length, settings.pair_code_ttl_seconds)
        store.save(state)
        print("Bridge unpaired.")
        print(f"New pairing code: {code}")
        return

    from .telegram_bridge import TelegramBridge

    bridge = TelegramBridge(settings=settings, store=store, state=state)
    asyncio.run(bridge.run())


if __name__ == "__main__":
    main()
