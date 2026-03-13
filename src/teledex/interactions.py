from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class InteractionOption:
    id: str
    label: str
    value: str


@dataclass(slots=True)
class InteractionPrompt:
    kind: str
    prompt: str
    options: list[InteractionOption]
    allow_custom: bool = False


QUESTION_HEADER_RE = re.compile(r"^(question|prompt|select|choose)\b", re.IGNORECASE)
OPTION_LINE_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+?)\s*$")


def detect_interaction_prompt(text: str) -> InteractionPrompt | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None

    lower = "\n".join(lines).lower()
    if "approval" in lower or "authorize" in lower or "permission" in lower:
        options = _extract_options(lines)
        if options:
            return InteractionPrompt(
                kind="approval",
                prompt=lines[0],
                options=options,
                allow_custom=True,
            )

    if any(QUESTION_HEADER_RE.search(line) for line in lines[:2]) or "recommended" in lower:
        options = _extract_options(lines)
        if options:
            return InteractionPrompt(
                kind="plan_question",
                prompt=lines[0],
                options=options,
                allow_custom=True,
            )
    return None


def _extract_options(lines: list[str]) -> list[InteractionOption]:
    options: list[InteractionOption] = []
    for index, line in enumerate(lines[1:], start=1):
        match = OPTION_LINE_RE.match(line)
        if not match:
            continue
        label = match.group(1)
        options.append(
            InteractionOption(
                id=f"opt{index}",
                label=label[:60],
                value=label,
            )
        )
    return options[:8]
