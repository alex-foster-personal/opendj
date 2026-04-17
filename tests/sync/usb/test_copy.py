"""Tests for apps.sync.usb.copy."""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

import pytest

from apps.sync.usb.copy import copy_one, delete_one, rename_one
from apps.sync.usb.diff import Op
from apps.sync.usb.profile import load_from_string


def _profile(policy: str = "canonical-wins"):
    return load_from_string(
        f"""
name: fx
drive_label: FIXTURE-A
playlists: [Warmup, Peak]
format: copy-as-is
layout: "Artist/Album/Track"
playlist_files: m3u8
conflict_policy: {policy}
"""
    )


def _op_for(src: Path, dst: Path) -> Op:
    return Op(
        kind="copy",
        dst=dst,
        dst_rel=PurePosixPath(dst.name),
        src=src,
        stable_id="sid",
        expected_hash="sha256:" + hashlib.sha256(src.read_bytes()).hexdigest(),
        reason="new",
        bytes_estimate=src.stat().st_size,
    )


@pytest.mark.requirement("CAT-02")
def test_copy_one_success(tmp_path) -> None:
    src = tmp_path / "src.mp3"
    src.write_bytes(b"data1234")
    dst = tmp_path / "out" / "dst.mp3"
    result = copy_one(_op_for(src, dst), _profile())
    assert result.ok
    assert dst.exists()
    assert dst.read_bytes() == b"data1234"
    # No leftover .part file.
    assert not dst.with_name(dst.name + ".part").exists()


@pytest.mark.requirement("CAT-02")
def test_copy_one_hash_mismatch_reports(tmp_path) -> None:
    src = tmp_path / "src.mp3"
    src.write_bytes(b"hello")
    dst = tmp_path / "out" / "dst.mp3"
    op = _op_for(src, dst)
    # Poison the expected hash.
    op_bad = Op(
        kind=op.kind,
        dst=op.dst,
        dst_rel=op.dst_rel,
        src=op.src,
        stable_id=op.stable_id,
        expected_hash="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        reason=op.reason,
        bytes_estimate=op.bytes_estimate,
    )
    result = copy_one(op_bad, _profile())
    assert not result.ok
    assert "post-write hash mismatch" in (result.error or "")


@pytest.mark.requirement("CAT-02")
def test_copy_backup_then_overwrite(tmp_path) -> None:
    src = tmp_path / "src.mp3"
    src.write_bytes(b"new data")
    dst = tmp_path / "out" / "dst.mp3"
    dst.parent.mkdir()
    dst.write_bytes(b"old")
    result = copy_one(_op_for(src, dst), _profile("backup-then-overwrite"))
    assert result.ok
    assert result.backup_path is not None
    assert result.backup_path.exists()
    assert result.backup_path.read_bytes() == b"old"
    assert dst.read_bytes() == b"new data"


@pytest.mark.requirement("CAT-02")
def test_copy_cleans_stale_part(tmp_path) -> None:
    src = tmp_path / "src.mp3"
    src.write_bytes(b"data")
    dst = tmp_path / "out" / "dst.mp3"
    dst.parent.mkdir()
    # Leftover .part from a crash.
    (dst.parent / (dst.name + ".part")).write_bytes(b"half")
    result = copy_one(_op_for(src, dst), _profile())
    assert result.ok
    assert not (dst.parent / (dst.name + ".part")).exists()


@pytest.mark.requirement("CAT-02")
def test_delete_one_success(tmp_path) -> None:
    f = tmp_path / "x.mp3"
    f.write_bytes(b"x")
    op = Op(
        kind="delete",
        dst=f,
        dst_rel=PurePosixPath("x.mp3"),
        src=None,
        stable_id=None,
        expected_hash=None,
        reason="not in profile",
        bytes_estimate=1,
    )
    r = delete_one(op)
    assert r.ok
    assert not f.exists()


@pytest.mark.requirement("CAT-02")
def test_delete_one_missing_is_noop(tmp_path) -> None:
    f = tmp_path / "absent.mp3"
    op = Op(
        kind="delete",
        dst=f,
        dst_rel=PurePosixPath("absent.mp3"),
        src=None,
        stable_id=None,
        expected_hash=None,
        reason="gone",
        bytes_estimate=0,
    )
    r = delete_one(op)
    assert r.ok


@pytest.mark.requirement("CAT-02")
def test_rename_one_moves_file(tmp_path) -> None:
    src = tmp_path / "drive" / "old" / "x.mp3"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"bytes")
    new_dst = tmp_path / "drive" / "new" / "x.mp3"
    op = Op(
        kind="rename",
        dst=new_dst,
        dst_rel=PurePosixPath("new/x.mp3"),
        src=src,
        stable_id="sid",
        expected_hash=None,
        reason="rename",
        bytes_estimate=5,
    )
    r = rename_one(op, from_path=src)
    assert r.ok
    assert new_dst.exists()
    assert not src.exists()
    # Regression for [C1]: rename_one must set backup_path so the reversal
    # script can emit an inverse `mv` restoring the original location.
    assert r.backup_path == src
