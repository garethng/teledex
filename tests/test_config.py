from pathlib import Path

from teledex.config import BotConfig, ConfigStore, Settings


def test_config_roundtrip(tmp_path: Path):
    config_path = tmp_path / "config.json"
    settings = Settings.defaults()

    bot = BotConfig(
        name="default",
        telegram_bot_token="token-123",
        agent_type="codex",
        workspace_root=tmp_path,
        memory_file=tmp_path / "Memory.md",
        agent_cmd="codex",
        session_storage_dir=tmp_path / "sessions",
        state_file=tmp_path / "state" / "default.json",
        log_file=tmp_path / "bot.log",
        model=None,
    )
    settings.bots.append(bot)

    store = ConfigStore(config_path)
    store.save(settings)
    loaded = store.load()

    assert len(loaded.bots) == 1
    loaded_bot = loaded.bots[0]
    assert loaded_bot.telegram_bot_token == "token-123"
    assert loaded_bot.workspace_root == tmp_path.resolve()
    assert loaded_bot.memory_file == (tmp_path / "Memory.md").resolve()
    assert loaded_bot.state_file == (tmp_path / "state" / "default.json").resolve()


def test_legacy_single_bot_format(tmp_path: Path):
    """Old single-bot config.json format is promoted to bots list."""
    import json

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "telegram_bot_token": "legacy-token",
            "codex_cmd": "codex",
            "workspace_root": str(tmp_path),
            "memory_file": str(tmp_path / "Memory.md"),
            "session_storage_dir": str(tmp_path / "sessions"),
            "state_file": str(tmp_path / "state.json"),
        }),
        encoding="utf-8",
    )

    store = ConfigStore(config_path)
    loaded = store.load()

    assert len(loaded.bots) == 1
    assert loaded.bots[0].telegram_bot_token == "legacy-token"
    assert loaded.bots[0].agent_type == "codex"
    assert loaded.bots[0].name == "default"


def test_multi_bot_config(tmp_path: Path):
    """Multiple bots with different agent types."""
    import json

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({
            "bots": [
                {
                    "name": "codex-bot",
                    "telegram_bot_token": "token-codex",
                    "agent_type": "codex",
                    "workspace_root": str(tmp_path),
                },
                {
                    "name": "opencode-bot",
                    "telegram_bot_token": "token-opencode",
                    "agent_type": "opencode",
                    "workspace_root": str(tmp_path),
                },
            ]
        }),
        encoding="utf-8",
    )

    store = ConfigStore(config_path)
    loaded = store.load()

    assert len(loaded.bots) == 2
    assert loaded.bots[0].name == "codex-bot"
    assert loaded.bots[0].agent_type == "codex"
    assert loaded.bots[1].name == "opencode-bot"
    assert loaded.bots[1].agent_type == "opencode"
