from pathlib import Path

from teledex.daemon import read_pid, remove_pid_file, write_pid


def test_pid_file_roundtrip(tmp_path: Path):
    pid_file = tmp_path / "teledex.pid"
    write_pid(pid_file, 12345)
    assert read_pid(pid_file) == 12345
    remove_pid_file(pid_file)
    assert read_pid(pid_file) is None
