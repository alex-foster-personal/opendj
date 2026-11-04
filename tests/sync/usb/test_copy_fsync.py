"""Platform-neutral tests for ``apps.sync.usb.copy._fsync_path``.

``test_copy.py`` is gated ``requires_darwin`` because the Pioneer USB actuator
flow is Mac-side; the durability primitive underneath it is shared by every
platform, so its regression tests live here and run everywhere.

The ``os.fsync`` spy calls straight through to the real syscall; it only
records which file each flushed descriptor belongs to, so a test can prove
the TARGET file was flushed rather than inferring it from intact bytes.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from apps.sync.usb import copy as copy_mod
from apps.sync.usb.copy import _fsync_path


def _spy_fsync(monkeypatch: pytest.MonkeyPatch) -> list[os.stat_result]:
    flushed: list[os.stat_result] = []
    real_fsync: Callable[[int], None] = os.fsync

    def _recording_fsync(fd: int) -> None:
        flushed.append(os.fstat(fd))
        real_fsync(fd)

    monkeypatch.setattr(copy_mod.os, "fsync", _recording_fsync)
    return flushed


def _was_flushed(flushed: list[os.stat_result], path: Path) -> bool:
    target = path.stat()
    return any(os.path.samestat(seen, target) for seen in flushed)


@pytest.mark.requirement("CAT-02")
def test_fsync_path_flushes_fresh_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a freshly written file is fsynced [then] that file is flushed, [else stop]."""
    # Regression: on Windows ``os.fsync`` maps to ``_commit``, which rejects a
    # read-only handle with EBADF, so every copy op was recorded as failed.
    fresh = tmp_path / "fresh.bin"
    fresh.write_bytes(b"payload")
    flushed = _spy_fsync(monkeypatch)
    _fsync_path(fresh)
    assert _was_flushed(flushed, fresh)
    assert fresh.read_bytes() == b"payload"


@pytest.mark.requirement("CAT-02")
def test_fsync_path_flushes_read_only_file_and_keeps_it_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a read-only file is fsynced [then] it is flushed and stays read-only, [else stop]."""
    # Regression: shutil.copy2 copies a read-only source's attribute, and on
    # Windows the write-handle open then failed and the flush was skipped.
    locked = tmp_path / "locked.bin"
    locked.write_bytes(b"payload")
    os.chmod(locked, stat.S_IREAD)
    flushed = _spy_fsync(monkeypatch)
    try:
        _fsync_path(locked)
        assert _was_flushed(flushed, locked)
        assert not locked.stat().st_mode & stat.S_IWRITE
        assert locked.read_bytes() == b"payload"
    finally:
        os.chmod(locked, stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="the POSIX branch is unchanged by the Windows fix")
@pytest.mark.requirement("CAT-02")
def test_fsync_path_absent_file_raises_on_windows(tmp_path: Path) -> None:
    """[if] a missing file is fsynced on Windows [then] the error propagates, [else stop]."""
    with pytest.raises(FileNotFoundError):
        _fsync_path(tmp_path / "absent.bin")
