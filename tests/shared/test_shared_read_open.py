"""The shared-read opener never pins the file's name (apps.shared.shared_read_open)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from apps.shared.shared_read_open import open_read_shared
from apps.stems.bundle_lock import replace_bundle_dir, swap_backup_path


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_an_open_reader_survives_a_bundle_swap_and_its_cleanup(tmp_path: Path) -> None:
    """A bundle swap renames the live dir aside and removes it while a stem streams.

    On Windows that rename fails with a sharing violation unless the
    descriptor was opened with FILE_SHARE_DELETE, and the cleanup must not
    fail the swap while the streamed file is delete-pending; on POSIX both
    always hold. The reader keeps the bytes it opened, the new bundle is in
    place, and the backup slot is free for the next swap.
    """
    live = tmp_path / "out" / "bundle"
    stem = _write(live / "vocals.m4a", b"old-bytes")
    staging = _write(tmp_path / "staging" / "bundle" / "vocals.m4a", b"new-bytes").parent
    descriptor = open_read_shared(stem)
    with os.fdopen(descriptor, "rb") as reader:
        replace_bundle_dir(staging, live)
        assert (live / "vocals.m4a").read_bytes() == b"new-bytes"
        assert reader.read() == b"old-bytes"
    assert not swap_backup_path(live).exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="creating a symlink needs Developer Mode or admin rights on Windows",
)
def test_a_symlink_is_never_followed(tmp_path: Path) -> None:
    target = _write(tmp_path / "real.m4a", b"elsewhere")
    link = tmp_path / "vocals.m4a"
    link.symlink_to(target)
    with pytest.raises(OSError):
        os.close(open_read_shared(link))
    # Control: the real file itself opens and reads.
    with os.fdopen(open_read_shared(target), "rb") as reader:
        assert reader.read() == b"elsewhere"


def test_a_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        open_read_shared(tmp_path / "absent.m4a")


def test_trash_an_earlier_swap_could_not_remove_is_swept_by_the_next(tmp_path: Path) -> None:
    """[if] an earlier swap left its old bundle as trash [then] the next swap removes it, [else stop].

    Control: a sibling that is not this bundle's trash is left alone.

    MUTATION TARGET: drop the ``_sweep_trash`` call in ``replace_bundle_dir``.
    """
    live = tmp_path / "out" / "bundle"
    _write(live / "vocals.m4a", b"old-bytes")
    backup = swap_backup_path(live)
    stale = _write(backup.with_name(f"{backup.name}.trash-deadbeef") / "vocals.m4a", b"stale").parent
    neighbor = _write(tmp_path / "out" / "other-bundle" / "vocals.m4a", b"keep").parent
    staging = _write(tmp_path / "staging" / "bundle" / "vocals.m4a", b"new-bytes").parent
    replace_bundle_dir(staging, live)
    assert not stale.exists()
    assert neighbor.exists()
    assert (live / "vocals.m4a").read_bytes() == b"new-bytes"


@pytest.mark.requirement("STEM-58")
def test_trash_that_still_cannot_be_removed_is_logged_and_never_fails_the_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[if] a deck still holds a trashed part [then] the sweep names that trash and the swap lands, [else stop].

    The sweep dropped such trash silently (Sol on #6820). The only double is
    the filesystem refusing ``rmtree`` for that one trash directory, as
    Windows does while a part in it is still streamed.

    MUTATION TARGETS: ``shutil.rmtree(stale, ignore_errors=True)`` (nothing
    is logged), or raise the cleanup error (the swap fails while a deck plays).
    """
    import logging
    import shutil

    live = tmp_path / "out" / "bundle"
    _write(live / "vocals.m4a", b"old-bytes")
    backup = swap_backup_path(live)
    stale = _write(backup.with_name(f"{backup.name}.trash-deadbeef") / "vocals.m4a", b"stale").parent
    staging = _write(tmp_path / "staging" / "bundle" / "vocals.m4a", b"new-bytes").parent
    real = shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path) == stale:
            raise PermissionError(32, "sharing violation", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    with caplog.at_level(logging.WARNING, logger="apps.stems.bundle_lock"):
        replace_bundle_dir(staging, live)

    assert (live / "vocals.m4a").read_bytes() == b"new-bytes"
    assert stale.is_dir(), "control: the refused trash really stayed"
    assert any(str(stale) in record.getMessage() for record in caplog.records), caplog.text
