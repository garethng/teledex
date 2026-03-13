from __future__ import annotations

import re


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")
STATUS_LINE_RE = re.compile(r"^\s*gpt-[\w.-]+\b.*left.*$", re.IGNORECASE)
PROMPT_ECHO_RE = re.compile(r"^\s*›\s*")
INLINE_STATUS_RE = re.compile(r"\s+gpt-[\w.-]+\b.*left.*$", re.IGNORECASE)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", OSC_RE.sub("", text)).replace("\r", "")


def normalize_codex_output(text: str, last_user_input: str | None = None) -> str:
    cleaned_lines: list[str] = []
    for raw_line in strip_ansi(text).splitlines():
        line = raw_line.replace("\x08", "").strip()
        if not line:
            continue
        if STATUS_LINE_RE.match(line):
            continue
        if _is_tui_chrome(line):
            continue
        line = _trim_inline_status(line)
        if last_user_input and line.startswith("›"):
            prompt_line = PROMPT_ECHO_RE.sub("", line).strip()
            prompt_line = _strip_prompt_echo(prompt_line, last_user_input)
            if not prompt_line:
                continue
            line = prompt_line
        elif last_user_input:
            line = _strip_prompt_echo(line, last_user_input)
        if line:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


def _is_tui_chrome(line: str) -> bool:
    if line.startswith(("╭", "╰", "│", "? for shortcuts")):
        return True
    lower = line.lower()
    return lower.startswith(
        (
            ">_ openai codex",
            "model:",
            "directory:",
            "tip:",
        )
    )


def _trim_inline_status(line: str) -> str:
    return INLINE_STATUS_RE.sub("", line).strip()


def _strip_prompt_echo(line: str, last_user_input: str) -> str:
    prompt = last_user_input.strip()
    if not prompt:
        return line.strip()
    if line == prompt:
        return ""
    if line.startswith(prompt):
        return line[len(prompt):].strip()
    return line.strip()


def chunk_text(text: str, limit: int = 3500) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()
    if text:
        chunks.append(text)
    return chunks


def normalize_telegram_slash_command(text: str) -> str:
    text = text.strip()
    if not text.startswith("/"):
        return text
    first, sep, rest = text.partition(" ")
    command, at, _bot = first.partition("@")
    command = _telegram_command_alias_to_codex(command)
    normalized = command
    if sep:
        normalized = f"{normalized} {rest.strip()}"
    return normalized.strip()


def _telegram_command_alias_to_codex(command: str) -> str:
    aliases = {
        "/sandbox_add_read_dir": "/sandbox-add-read-dir",
        "/debug_config": "/debug-config",
    }
    return aliases.get(command, command)
