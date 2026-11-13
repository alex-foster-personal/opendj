"""Engine lock peek under a Windows-style data directory (issue #13427).

The packaged Windows engine keeps ``.engine.lock`` under
``%LOCALAPPDATA%\\com.opendj.desktop``. These tests rebuild that layout under
``tmp_path`` (a real Windows path on a Windows runner, a nested POSIX dir
elsewhere) and pin that ``read_holder`` and ``inspect_lock`` stay read-only
and tolerant there: they never rewrite the record, and an empty or malformed
body is "no holder", not a crash.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path, PureWindowsPath

import pytest

from apps.engine_core import lock as lock_module
from apps.engine_core.lock import EngineLock, EngineLockError, inspect_lock, read_holder
from apps.shared.engine_data_dir import engine_data_dir

_WINDOWS_LOCAL_APP_DATA = r"C:\Users\dev\AppData\Local"

_CHILD_HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    from apps.engine_core.lock import EngineLock

    lock = EngineLock(Path(sys.argv[1]), host="127.0.0.1", port=8686, boot_id="child-boot")
    lock.acquire()
    print("held", flush=True)
    sys.stdin.read()
    lock.release()
    """
)


def _windows_style_data_dir(root: Path) -> Path:
    """Rebuild the packaged Windows engine data dir (minus its drive) under ``root``."""
    resolved = engine_data_dir(platform="win32", env={"LOCALAPPDATA": _WINDOWS_LOCAL_APP_DATA})
    parts = PureWindowsPath(str(resolved)).parts[1:]
    assert parts[-1] == "com.opendj.desktop"
    data_dir = root.joinpath(*parts)
    data_dir.mkdir(parents=True)
    return data_dir


def _snapshot(path: Path) -> tuple[bytes, int]:
    """Bytes and mtime of the lock file, to prove a peek wrote nothing."""
    return path.read_bytes(), path.stat().st_mtime_ns


def test_windows_style_dir_valid_payload_is_read_without_writing(tmp_path: Path) -> None:
    """
    [if] a Windows-style data directory has a valid lock payload [then] read_holder and inspect_lock name the holder and leave the file untouched, [else stop].
    """
    lock_path = _windows_style_data_dir(tmp_path) / ".engine.lock"
    payload = {"pid": 4321, "port": 8585, "host": "127.0.0.1", "boot_id": "boot-win"}
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    before = _snapshot(lock_path)

    holder = read_holder(lock_path)
    assert holder is not None
    assert (holder.pid, holder.port, holder.host, holder.boot_id) == (4321, 8585, "127.0.0.1", "boot-win")
    assert inspect_lock(lock_path) == "free"
    assert _snapshot(lock_path) == before


@pytest.mark.parametrize("body", [b"", b" " * lock_module.RECORD_BYTES, b'{"pid": 12', b"[1, 2]", b"\xff\xfe"])
def test_windows_style_dir_empty_or_malformed_payload_is_no_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    """
    [if] the lock payload is empty or malformed [then] read_holder returns None, inspect_lock reports free, and the bytes are unchanged, [else stop].
    """
    monkeypatch.setattr(lock_module.time, "sleep", lambda _s: None)
    lock_path = _windows_style_data_dir(tmp_path) / ".engine.lock"
    lock_path.write_bytes(body)
    before = _snapshot(lock_path)

    assert read_holder(lock_path) is None
    assert inspect_lock(lock_path) == "free"
    assert _snapshot(lock_path) == before


def test_windows_style_dir_lock_held_by_another_process_is_peeked_not_taken(tmp_path: Path) -> None:
    """
    [if] another process retains the exclusive lock [then] the peek names that process, does not steal or rewrite the lock, and a second acquire still refuses, [else stop].
    """
    lock_path = _windows_style_data_dir(tmp_path) / ".engine.lock"
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_HOLDER, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "held"
        before = _snapshot(lock_path)

        peeked = read_holder(lock_path)
        assert peeked is not None
        assert (peeked.pid, peeked.port, peeked.boot_id) == (child.pid, 8686, "child-boot")
        inspected = inspect_lock(lock_path)
        assert inspected != "free"
        assert inspected.pid == child.pid
        assert _snapshot(lock_path) == before

        with pytest.raises(EngineLockError, match="child-boot"):
            EngineLock(lock_path, boot_id="parent-boot").acquire()
        assert child.poll() is None
    finally:
        child.communicate(input="", timeout=10)
    assert child.returncode == 0
    assert os.path.isfile(lock_path)
    assert inspect_lock(lock_path) == "free"
