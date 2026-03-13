from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from .interactions import InteractionPrompt, detect_interaction_prompt


logger = logging.getLogger(__name__)
STRIPPED_ENV_KEYS = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "WARP_HONOR_PS1",
    "WARP_IS_LOCAL_SHELL_SESSION",
    "WARP_USE_SSH_WRAPPER",
    "__CFBundleIdentifier",
    "CODEX_API_KEY",
    "CODEX_THREAD_ID",
    "CODEX_CI",
}


@dataclass(slots=True)
class SessionStatus:
    state: str
    pid: int | None
    started_at: float | None
    last_output_at: float | None
    session_dir: Path | None
    pending_interaction: InteractionPrompt | None


class CodexSession:
    def __init__(
        self,
        codex_cmd: str,
        root_dir: Path,
        workspace_root: Path,
        memory_file: Path,
        on_text,
        on_interaction,
    ):
        self.codex_cmd = codex_cmd
        self.root_dir = root_dir
        self.workspace_root = workspace_root
        self.memory_file = memory_file
        self.on_text = on_text
        self.on_interaction = on_interaction
        self.process: asyncio.subprocess.Process | None = None
        self.started_at: float | None = None
        self.last_output_at: float | None = None
        self.session_dir: Path | None = None
        self.pending_interaction: InteractionPrompt | None = None
        self.thread_id: str | None = None

    async def start(self) -> None:
        if self.session_dir is None:
            self.session_dir = self.root_dir / time.strftime("%Y%m%d-%H%M%S")
            self.session_dir.mkdir(parents=True, exist_ok=True)
        if self.started_at is None:
            self.started_at = time.time()

    async def stop(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None
        self.pending_interaction = None

    async def reset(self) -> None:
        await self.stop()
        self.thread_id = None
        self.started_at = None
        self.last_output_at = None
        self.session_dir = None
        await self.start()

    async def send_text(self, text: str) -> None:
        await self.start()
        if self.process and self.process.returncode is None:
            raise RuntimeError("Codex is already processing another request")
        prompt = self._compose_prompt(text.rstrip("\n"))
        self.pending_interaction = None
        child_env = {
            key: value
            for key, value in os.environ.items()
            if key not in STRIPPED_ENV_KEYS
        }
        args = self._build_command(prompt)
        logger.info("Running codex command: %s", " ".join(args[:4]))
        self.process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(self.workspace_root),
            env=child_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        await self._collect_response()

    async def interrupt(self) -> None:
        if not self.process or self.process.returncode is not None:
            return
        os.killpg(os.getpgid(self.process.pid), signal.SIGINT)

    def status(self) -> SessionStatus:
        state = "dead"
        if self.process and self.process.returncode is None:
            state = "busy"
        elif self.started_at is not None:
            state = "awaiting_structured_reply" if self.pending_interaction else "ready"
        return SessionStatus(
            state=state,
            pid=self.process.pid if self.process else None,
            started_at=self.started_at,
            last_output_at=self.last_output_at,
            session_dir=self.session_dir,
            pending_interaction=self.pending_interaction,
        )

    def _build_command(self, prompt: str) -> list[str]:
        if self.thread_id:
            return [
                self.codex_cmd,
                "exec",
                "resume",
                "--skip-git-repo-check",
                "--json",
                self.thread_id,
                prompt,
            ]
        return [
            self.codex_cmd,
            "exec",
            "--skip-git-repo-check",
            "--json",
            "--color",
            "never",
            prompt,
        ]

    async def _collect_response(self) -> None:
        if not self.process or self.process.stdout is None or self.process.stderr is None:
            return
        stdout_task = asyncio.create_task(self._read_stdout(self.process.stdout))
        stderr_task = asyncio.create_task(self._read_stderr(self.process.stderr))
        await self.process.wait()
        await asyncio.gather(stdout_task, stderr_task)
        self.process = None

    async def _read_stdout(self, stream: asyncio.StreamReader) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            self.last_output_at = time.time()
            raw = line.decode("utf-8", "ignore").strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("Ignoring non-JSON stdout from codex: %s", raw)
                continue
            await self._handle_event(event)

    async def _read_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            raw = line.decode("utf-8", "ignore").strip()
            if raw:
                logger.warning("codex stderr: %s", raw)

    async def _handle_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "thread.started":
            self.thread_id = event.get("thread_id") or self.thread_id
            return
        if event_type == "item.completed":
            item = event.get("item", {})
            if item.get("type") != "agent_message":
                return
            text = str(item.get("text", "")).strip()
            if not text:
                return
            interaction = detect_interaction_prompt(text)
            if interaction:
                self.pending_interaction = interaction
                await self.on_interaction(interaction, text)
            else:
                await self.on_text(text)
            return
        if event_type == "error":
            message = str(event.get("message", "")).strip()
            if message:
                await self.on_text(f"Codex error: {message}")

    def _compose_prompt(self, user_text: str) -> str:
        memory_text = self._read_memory_file()
        if not memory_text:
            return user_text
        return (
            "Use the following persistent project memory as additional context for this request.\n"
            "Treat it as user-maintained context that may be relevant, but do not quote it unless needed.\n\n"
            "<memory>\n"
            f"{memory_text}\n"
            "</memory>\n\n"
            "<user_request>\n"
            f"{user_text}\n"
            "</user_request>"
        )

    def _read_memory_file(self) -> str:
        if not self.memory_file.exists():
            return ""
        try:
            return self.memory_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("Failed to read memory file %s: %s", self.memory_file, exc)
            return ""
