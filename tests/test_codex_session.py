from pathlib import Path

from teledex.codex_session import CodexSession


async def _noop_text(_: str) -> None:
    return None


async def _noop_interaction(*_) -> None:
    return None


def test_compose_prompt_includes_memory(tmp_path: Path):
    memory_file = tmp_path / "Memory.md"
    memory_file.write_text("project memory", encoding="utf-8")
    session = CodexSession(
        codex_cmd="codex",
        root_dir=tmp_path / "sessions",
        workspace_root=tmp_path,
        memory_file=memory_file,
        on_text=_noop_text,
        on_interaction=_noop_interaction,
    )
    prompt = session._compose_prompt("fix the bug")
    assert "project memory" in prompt
    assert "fix the bug" in prompt


def test_compose_prompt_without_memory_is_plain_text(tmp_path: Path):
    session = CodexSession(
        codex_cmd="codex",
        root_dir=tmp_path / "sessions",
        workspace_root=tmp_path,
        memory_file=tmp_path / "Memory.md",
        on_text=_noop_text,
        on_interaction=_noop_interaction,
    )
    assert session._compose_prompt("hello") == "hello"
