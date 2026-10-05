"""CLI entry that used to write BPM and key into audio files.

That write path depended on ``mutagen`` (GPL-2.0-or-later). This Apache-2.0
product does not depend on it. ``python -m apps.analysis.write_tags`` exits
2 and does not modify any file.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from apps.shared.file_rewrite import keep_ownership
from apps.shared.tag_writer import TAG_WRITE_REMOVED, TagWriteRemoved

log_name = "apps.analysis.write_tags"


def _refuse(*_args: object, **_kwargs: object) -> None:
    raise TagWriteRemoved(TAG_WRITE_REMOVED)


def _atomic_write_tags(path: Path, tags: object) -> None:
    """Rewrite tags, then hand the inode back to its previous owner.

    ``_write_tags`` is the removed mutagen writer (it raises). Callers that
    supply a writer still abort when the owner cannot be kept, and the
    original bytes stay in place with no temp file left behind.
    """
    path = Path(path)
    original = path.read_bytes()
    before = path.stat()
    try:
        _write_tags(path, tags)
        keep_ownership(before, path)
    except BaseException:
        path.write_bytes(original)
        for leftover in path.parent.glob(f".{path.name}.tag-*"):
            leftover.unlink(missing_ok=True)
        raise


def _restore_from_snapshot(snapshot: Path, track: Path) -> None:
    """Put ``snapshot``'s bytes back on ``track``.

    A refused chown does not block the restore: the half-written file must
    not stay. The temp (``.name.restore-*``) is removed either way.
    """
    snapshot = Path(snapshot)
    track = Path(track)
    data = snapshot.read_bytes()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{track.name}.restore-", dir=str(track.parent))
    tmp = Path(tmp_name)
    try:
        os.write(fd, data)
        os.close(fd)
        fd = -1
        try:
            keep_ownership(snapshot.stat(), tmp)
        except PermissionError:
            pass
        os.replace(tmp, track)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise


_write_tags = _refuse
apply_writes = _refuse


def main(argv: list[str] | None = None) -> int:
    """Print the removal reason and exit 2. ``argv`` is ignored."""
    del argv
    print(TAG_WRITE_REMOVED, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
