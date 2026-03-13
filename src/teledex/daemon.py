from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def read_pid(pid_file: Path) -> int | None:
    if not pid_file.exists():
        return None
    raw = pid_file.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def is_process_running(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def write_pid(pid_file: Path, pid: int) -> None:
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{pid}\n", encoding="utf-8")


def remove_pid_file(pid_file: Path) -> None:
    try:
        pid_file.unlink()
    except FileNotFoundError:
        return


def start_background_process(log_file: Path) -> int:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as stream:
        process = subprocess.Popen(
            [sys.executable, "-m", "teledex.cli", "_serve"],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            start_new_session=True,
            close_fds=True,
        )
    return process.pid


def stop_process(pid_file: Path, timeout_seconds: float = 10.0) -> bool:
    pid = read_pid(pid_file)
    if pid is None:
        remove_pid_file(pid_file)
        return False
    if not is_process_running(pid):
        remove_pid_file(pid_file)
        return False
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not is_process_running(pid):
            remove_pid_file(pid_file)
            return True
        time.sleep(0.2)
    os.kill(pid, signal.SIGKILL)
    remove_pid_file(pid_file)
    return True
