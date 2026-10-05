"""A tag rewrite keeps the file's owner and group (Codex on #4974, 8a6631424).

``mkstemp`` creates the replacement inode as the writing user, and
``os.replace`` keeps that inode, so a rewrite that did not hand the file back
would silently transfer a shared-library track to whoever edited its tags.

[if] a tag rewrite replaces a file another user owns [then] the new file keeps that owner and group or the write aborts, [else stop].

A rollback that cannot keep the owner still restores the audio bytes.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from apps.analysis import write_tags as wt
from apps.shared import file_rewrite as fr
from apps.shared.file_rewrite import copy_rest, keep_ownership, rewrite_atomic

pytestmark = pytest.mark.requirement("TAGIO-02")

needs_chown = pytest.mark.skipif(not hasattr(os, "chown"), reason="no os.chown: Windows takes the owner from the directory")


def _stat_owned_by(path: Path, uid: int, gid: int) -> os.stat_result:
    fields = list(path.stat())
    fields[4], fields[5] = uid, gid  # st_uid, st_gid
    return os.stat_result(fields)


def _build(src: BinaryIO, out: BinaryIO) -> None:
    out.write(b"NEWTAG")
    copy_rest(src, out, 6)


def _refuse(*_args: Any) -> None:
    raise PermissionError(1, "Operation not permitted")


@needs_chown
def test_a_replacement_owned_by_the_writer_is_handed_back(tmp_path: Path, monkeypatch):
    """[if] the original belongs to another user [then] the replacement is chowned to that user and group, [else stop].

    MUTATION TARGET: drop the ``os.chown`` in ``keep_ownership`` and nothing is handed back.
    """
    tmp = tmp_path / "tmp.mp3"
    tmp.write_bytes(b"x")
    calls: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: calls.append((Path(path), uid, gid)))

    keep_ownership(_stat_owned_by(tmp, 4242, 4343), tmp)

    assert calls == [(tmp, 4242, 4343)]


@needs_chown
def test_a_replacement_already_owned_right_is_left_alone(tmp_path: Path, monkeypatch):
    """[if] the owner already matches [then] no chown is attempted, [else stop].

    Overshoot control: an unconditional chown fails for a non-root writer
    on group changes it may not make, blocking every ordinary tag write.
    """
    tmp = tmp_path / "tmp.mp3"
    tmp.write_bytes(b"x")
    monkeypatch.setattr(os, "chown", _refuse)

    keep_ownership(tmp.stat(), tmp)  # does not raise


@needs_chown
def test_a_group_the_writer_may_not_set_does_not_block_its_own_file(tmp_path: Path, monkeypatch):
    """[if] only the group differs and the writer may not set it [then] the group change is skipped and the write proceeds, [else stop].

    Overshoot control: aborting here would block every tag edit on the
    writer's own files in a setgid directory, or on macOS, where a new file
    always takes the directory's group.
    """
    tmp = tmp_path / "tmp.mp3"
    tmp.write_bytes(b"x")
    calls: list[tuple[int, int]] = []

    def chown(_path: object, uid: int, gid: int) -> None:
        calls.append((uid, gid))
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chown", chown)
    mine = tmp.stat()

    keep_ownership(_stat_owned_by(tmp, mine.st_uid, mine.st_gid + 1), tmp)  # does not raise

    assert calls == [(-1, mine.st_gid + 1)], "the group change is still attempted"


@needs_chown
def test_a_file_another_user_owns_still_aborts_when_it_cannot_be_given_back(tmp_path: Path, monkeypatch):
    """[if] the owner differs and cannot be kept [then] ``keep_ownership`` raises, [else stop].

    Control for the test above: tolerating every refused chown would let a
    rewrite hand another user's file to the writer again.
    """
    tmp = tmp_path / "tmp.mp3"
    tmp.write_bytes(b"x")
    monkeypatch.setattr(os, "chown", _refuse)
    mine = tmp.stat()

    with pytest.raises(PermissionError):
        keep_ownership(_stat_owned_by(tmp, mine.st_uid + 1, mine.st_gid), tmp)


def test_a_rewrite_that_cannot_keep_the_owner_aborts_and_keeps_the_original(tmp_path: Path, monkeypatch):
    """[if] the owner cannot be kept [then] ``rewrite_atomic`` raises, the original is untouched and no temp file is left, [else stop].

    MUTATION TARGET: drop the ``keep_ownership`` call in ``rewrite_atomic``
    and the write lands with the writer as the new owner.
    """
    track = tmp_path / "track.mp3"
    track.write_bytes(b"OLDTAG" + b"audio-bytes")
    monkeypatch.setattr(fr, "keep_ownership", _refuse)

    with pytest.raises(PermissionError):
        rewrite_atomic(track, _build)

    assert track.read_bytes() == b"OLDTAG" + b"audio-bytes"
    assert [p.name for p in tmp_path.iterdir()] == ["track.mp3"]


def test_an_analysis_tag_write_that_cannot_keep_the_owner_aborts(tmp_path: Path, monkeypatch):
    """[if] the analysis write-back cannot keep the owner [then] it raises and the original is untouched, [else stop].

    MUTATION TARGET: drop the ``keep_ownership`` call in ``_atomic_write_tags``.
    """
    track = tmp_path / "track.flac"
    track.write_bytes(b"original")
    monkeypatch.setattr(wt, "_write_tags", lambda path, _new: Path(path).write_bytes(b"rewritten"))
    monkeypatch.setattr(wt, "keep_ownership", _refuse)

    with pytest.raises(PermissionError):
        wt._atomic_write_tags(track, {"k": "v"})

    assert track.read_bytes() == b"original"
    assert [p.name for p in tmp_path.iterdir()] == ["track.flac"]


def test_a_rollback_restores_the_bytes_when_the_owner_cannot_be_kept(tmp_path: Path, monkeypatch):
    """[if] a rollback cannot keep the owner [then] the snapshot's bytes are still restored, [else stop].

    Overshoot control for the abort above: a rollback that refused would
    leave the half-written file in place.
    """
    track = tmp_path / "track.flac"
    snapshot = tmp_path / "snap.flac"
    snapshot.write_bytes(b"original")
    track.write_bytes(b"half-written")
    monkeypatch.setattr(wt, "keep_ownership", _refuse)

    wt._restore_from_snapshot(snapshot, track)

    assert track.read_bytes() == b"original"
    assert not list(tmp_path.glob(".track.flac.restore-*"))
