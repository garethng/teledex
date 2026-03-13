from pathlib import Path

from teledex.config import ConfigStore, Settings


def test_config_roundtrip(tmp_path: Path):
    config_path = tmp_path / "config.json"
    settings = Settings.defaults()
    settings.telegram_bot_token = "token-123"
    settings.workspace_root = tmp_path
    settings.memory_file = tmp_path / "Memory.md"
    settings.state_file = tmp_path / "state.json"
    settings.session_storage_dir = tmp_path / "sessions"

    store = ConfigStore(config_path)
    store.save(settings)
    loaded = store.load()

    assert loaded.telegram_bot_token == "token-123"
    assert loaded.workspace_root == tmp_path.resolve()
    assert loaded.memory_file == (tmp_path / "Memory.md").resolve()
    assert loaded.state_file == (tmp_path / "state.json").resolve()
