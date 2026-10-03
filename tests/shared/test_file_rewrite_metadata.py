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


def _fake_xattrs(monkeypatch, names: list[str], *, refuse: str, code: int) -> None:
    """Stand in for the kernel: ``names`` listed on the source, ``refuse``
    rejected on set with ``code``. The three calls are replaced together so
    the test asks one question: what a refused set does to the write."""
    monkeypatch.setattr(os, "listxattr", lambda path: list(names), raising=False)
    monkeypatch.setattr(os, "getxattr", lambda path, name: b"v", raising=False)

    def setxattr(path, name, value):
        if name == refuse:
            raise OSError(code, os.strerror(code))

    monkeypatch.setattr(os, "setxattr", setxattr, raising=False)
    monkeypatch.setattr("apps.shared.file_rewrite.sys.platform", "linux")


def _build(src: BinaryIO, out: BinaryIO) -> None:
    out.write(b"NEWTAG")
    copy_rest(src, out, 6)


@pytest.mark.requirement("TAGIO-02")
def test_a_policy_xattr_the_writer_may_not_set_never_blocks_the_tag_write(tmp_path: Path, monkeypatch):
    """[if] a non-root writer cannot set a ``security.*`` attribute on the new file [then] the tag write still lands, [else stop].

    MUTATION TARGET: raise on every refused set and an SELinux-labelled or
    capability-bearing file could never have its tags written.
    """
    track = tmp_path / "track.mp3"
    track.write_bytes(b"OLDTAG" + b"audio-bytes")
    _fake_xattrs(monkeypatch, ["security.capability", "user.kept"], refuse="security.capability", code=errno.EPERM)

    rewrite_atomic(track, _build)

    assert track.read_bytes() == b"NEWTAG" + b"audio-bytes"


@pytest.mark.requirement("TAGIO-02")
def test_a_user_xattr_that_cannot_be_copied_aborts_and_keeps_the_original(tmp_path: Path, monkeypatch):
    """[if] a ``user.*`` attribute cannot be set on the new file [then] the write aborts and the original is untouched, [else stop].

    Overshoot control for the test above: skipping every refused set would
    pass it and silently drop the attributes this writer promises to keep.
    """
    track = tmp_path / "track.mp3"
    track.write_bytes(b"OLDTAG" + b"audio-bytes")
    _fake_xattrs(monkeypatch, ["user.kept"], refuse="user.kept", code=errno.EPERM)

    with pytest.raises(PermissionError):
        rewrite_atomic(track, _build)

    assert track.read_bytes() == b"OLDTAG" + b"audio-bytes"
    assert [p.name for p in tmp_path.iterdir()] == ["track.mp3"], "the temp file is removed"
