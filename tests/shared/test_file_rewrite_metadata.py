"""A tag rewrite keeps the file's extended attributes (Codex on #4974, 3e9a4592b).

``rewrite_atomic`` swaps in a new inode, so anything not copied across is
lost: Finder tags, quarantine flags and, on Linux, POSIX ACLs (which live in
xattrs). The writer promises to change tag blocks only.

* [if] a file carrying an xattr is rewritten [then] the new file carries the
  same xattr and the new bytes, [else stop].
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import BinaryIO

import pytest

from apps.shared.file_rewrite import copy_rest, rewrite_atomic


def _set_user_xattr_or_skip(path: Path) -> None:
    if not hasattr(os, "setxattr"):
        pytest.skip("no os.setxattr on this platform: macOS takes the copyfile(3) branch, Windows has no xattrs")
    try:
        getattr(os, "setxattr")(path, "user.opendj.test", b"keep-me")  # noqa: B009 - absent on macOS
    except OSError as exc:
        if exc.errno in {errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
            pytest.skip(f"the test filesystem under {path.parent} has no user xattrs")
        raise


@pytest.mark.requirement("TAGIO-02")
def test_rewrite_keeps_extended_attributes(tmp_path: Path):
    """[if] a file with an xattr is rewritten [then] the xattr survives the swap, [else stop].

    MUTATION TARGET: drop the ``copy_extended_metadata`` call in
    ``rewrite_atomic`` and the attribute is gone after the replace.
    """
    track = tmp_path / "track.mp3"
    track.write_bytes(b"OLDTAG" + b"audio-bytes")
    _set_user_xattr_or_skip(track)

    def build(src: BinaryIO, out: BinaryIO) -> None:
        out.write(b"NEWTAG")
        copy_rest(src, out, 6)

    rewrite_atomic(track, build)

    assert track.read_bytes() == b"NEWTAG" + b"audio-bytes"
    assert getattr(os, "getxattr")(track, "user.opendj.test") == b"keep-me"  # noqa: B009 - absent on macOS
