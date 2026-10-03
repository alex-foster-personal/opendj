"""Regression for Codex P06-F01 / META-02.

Live tag writes must be atomic + recoverable: a mid-write failure must
leave the audio file byte-identical to its pre-write state. Prior to the
fix, ``_write_tags`` mutated the file in place and a later
``_read_current_tags`` / verify failure left the file partially rewritten
with no way back.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from apps.analysis import write_tags as wt
from apps.analysis.record import AnalysisRecord

pytestmark = pytest.mark.requirement("META-02")

#: ``os`` untyped for the xattr calls: typeshed omits them on darwin.
_OS: Any = os


def _rec(sid: str) -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=sid,
        backend="librosa+madmom",
        backend_version="librosa==0.10.2+madmom==0.17.dev",
        analyzed_at=datetime(2026, 4, 17, tzinfo=UTC),
        duration_s=10.0, sample_rate=44100,
        bpm=128.12, bpm_confidence=0.9,
        key_camelot="8A", key_openkey="1m", key_confidence=0.9,
        energy=7,
    )


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_atomic_write_replaces_and_leaves_no_tmp(
    flac_fixture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wt, "BACKUP_ROOT", tmp_path / "tb")
    monkeypatch.setattr(wt, "REVERSAL_ROOT", tmp_path / "rv")
    monkeypatch.setattr(wt, "FILE_BACKUP_ROOT", tmp_path / "fb")

    delta = wt.TagDelta(
        path=flac_fixture, stable_id="atomic-ok",
        old=wt._read_current_tags(flac_fixture),
        new=wt._build_new_tags(_rec("atomic-ok")),
    )
    s = wt.apply_writes([delta], live=True, bulk=False)
    assert s.written == 1 and s.failed == 0

    # No stray .opendj-*/restore-* tmp siblings left behind in the
    # audio file's directory.
    strays = [
        p for p in flac_fixture.parent.iterdir()
        if p.name.startswith(f".{flac_fixture.name}.")
    ]
    assert strays == [], f"atomic write left tmp files: {strays}"

    got = wt._read_current_tags(flac_fixture)
    assert got["INITIALKEY"] == "8A"


def test_failed_verify_restores_original_bytes(
    flac_fixture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a post-write verify failure: file bytes must be restored."""
    monkeypatch.setattr(wt, "BACKUP_ROOT", tmp_path / "tb")
    monkeypatch.setattr(wt, "REVERSAL_ROOT", tmp_path / "rv")
    monkeypatch.setattr(wt, "FILE_BACKUP_ROOT", tmp_path / "fb")

    pre_digest = _sha(flac_fixture)
    pre_tags = wt._read_current_tags(flac_fixture)

    # Break the post-write verification path so apply_writes enters the
    # except branch after the atomic replace has already landed.
    def boom(_path: Path) -> dict[str, str]:
        raise RuntimeError("synthetic verify failure")

    monkeypatch.setattr(wt, "_read_current_tags", boom)

    delta = wt.TagDelta(
        path=flac_fixture, stable_id="restore-me",
        old=pre_tags,
        new=wt._build_new_tags(_rec("restore-me")),
    )
    s = wt.apply_writes([delta], live=True, bulk=False)
    assert s.failed == 1 and s.written == 0

    # Restore the real reader so we can re-inspect the file.
    monkeypatch.undo()

    # Byte-identical restore from the pre-write snapshot.
    assert _sha(flac_fixture) == pre_digest, (
        "expected file to be restored to pre-write bytes after failure"
    )

    # Snapshot copy must be on disk for manual rollback/forensics.
    snaps = list((tmp_path / "fb").glob("restore-me-*"))
    assert snaps, "pre-write snapshot was not retained"


def test_write_tags_failure_leaves_original_intact(
    flac_fixture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the tag writer itself raises, the original file must be untouched."""
    monkeypatch.setattr(wt, "BACKUP_ROOT", tmp_path / "tb")
    monkeypatch.setattr(wt, "REVERSAL_ROOT", tmp_path / "rv")
    monkeypatch.setattr(wt, "FILE_BACKUP_ROOT", tmp_path / "fb")

    pre_digest = _sha(flac_fixture)

    def explode(_path: Path, _new: dict[str, str]) -> None:
        raise RuntimeError("synthetic tag writer failure")

    monkeypatch.setattr(wt, "_write_tags", explode)

    delta = wt.TagDelta(
        path=flac_fixture, stable_id="mw-fail",
        old={}, new=wt._build_new_tags(_rec("mw-fail")),
    )
    s = wt.apply_writes([delta], live=True, bulk=False)
    assert s.failed == 1 and s.written == 0

    # Atomic semantics: the in-place file was never touched because the
    # mutation happened on the tmp copy.
    assert _sha(flac_fixture) == pre_digest

    # No stray tmp siblings.
    strays = [
        p for p in flac_fixture.parent.iterdir()
        if p.name.startswith(f".{flac_fixture.name}.")
    ]
    assert strays == []


@pytest.mark.requirement("TAGIO-02")
def test_the_outer_swap_carries_the_original_files_metadata(
    flac_fixture: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a tag write swaps its temp copy over the original [then] the original's ACL and xattrs are copied onto it first, [else stop].

    ``shutil.copy2`` keeps neither on macOS, so without the seam call a
    successful write changes who can read the library file. MUTATION TARGET:
    drop ``copy_extended_metadata`` before the ``os.replace``.
    """
    calls: list[tuple[Path, Path, bool]] = []

    def spy(src: Path, dst: Path) -> None:
        calls.append((Path(src), Path(dst), Path(dst).exists()))

    monkeypatch.setattr(wt, "copy_extended_metadata", spy)
    wt._atomic_write_tags(flac_fixture, wt._build_new_tags(_rec("acl")))
    assert [(src, live) for src, _dst, live in calls] == [(flac_fixture, True)]
    assert calls[0][1].parent == flac_fixture.parent and calls[0][1] != flac_fixture
    assert wt._read_current_tags(flac_fixture)["INITIALKEY"] == "8A"


@pytest.mark.requirement("TAGIO-02")
def test_a_rollback_carries_the_live_files_metadata(
    flac_fixture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a failed write restores the snapshot [then] the live file's ACL and xattrs are copied onto the restored copy first, [else stop].

    The snapshot is a ``copy2`` copy, which keeps no ACL on macOS.
    MUTATION TARGET: drop ``copy_extended_metadata`` in ``_restore_from_snapshot``.
    """
    snapshot = tmp_path / "snap.flac"
    snapshot.write_bytes(flac_fixture.read_bytes())
    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr(
        wt, "copy_extended_metadata", lambda src, dst: calls.append((Path(src), Path(dst).exists()))
    )
    wt._restore_from_snapshot(snapshot, flac_fixture)
    assert calls == [(flac_fixture, True)]


def _user_xattrs_supported(path: Path) -> bool:
    try:
        _OS.setxattr(path, "user.opendj.probe", b"1")
        _OS.removexattr(path, "user.opendj.probe")
    except (AttributeError, OSError):  # AttributeError: no xattr API on this platform
        return False
    return True


@pytest.mark.requirement("TAGIO-02")
def test_the_reversal_script_keeps_the_live_files_xattrs(
    flac_fixture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the standalone reversal script restores the snapshot [then] the live file's xattrs survive, [else stop].

    MUTATION TARGET: drop ``keep_metadata`` from the generated script.
    """
    if not _user_xattrs_supported(flac_fixture):
        pytest.skip("this filesystem has no user xattrs to keep")
    monkeypatch.setattr(wt, "BACKUP_ROOT", tmp_path / "tb")
    monkeypatch.setattr(wt, "REVERSAL_ROOT", tmp_path / "rv")
    monkeypatch.setattr(wt, "FILE_BACKUP_ROOT", tmp_path / "fb")
    delta = wt.TagDelta(
        path=flac_fixture, stable_id="rev-xattr",
        old=wt._read_current_tags(flac_fixture), new=wt._build_new_tags(_rec("rev-xattr")),
    )
    s = wt.apply_writes([delta], live=True, bulk=False)
    _OS.setxattr(flac_fixture, "user.opendj.tag", b"kept")  # set after the snapshot
    r = subprocess.run([sys.executable, str(s.reversal_scripts[0])], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert _OS.getxattr(flac_fixture, "user.opendj.tag") == b"kept"


@pytest.mark.requirement("TAGIO-02")
def test_a_rollback_restores_the_bytes_when_metadata_cannot_be_kept(
    flac_fixture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the metadata copy fails during a rollback [then] the snapshot's bytes are still restored and no temp file is left, [else stop].

    MUTATION TARGET: let ``copy_extended_metadata``'s OSError escape
    ``_restore_from_snapshot`` again. The control is
    ``test_a_rollback_carries_the_live_files_metadata``, which still requires
    the copy to be attempted.
    """
    original = flac_fixture.read_bytes()
    snapshot = tmp_path / "snap.flac"
    snapshot.write_bytes(original)
    flac_fixture.write_bytes(b"half-written")

    def refuse(_src: Path, _dst: Path) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(wt, "copy_extended_metadata", refuse)
    wt._restore_from_snapshot(snapshot, flac_fixture)
    assert flac_fixture.read_bytes() == original
    assert not list(flac_fixture.parent.glob(f".{flac_fixture.name}.restore-*"))


@pytest.mark.requirement("TAGIO-02")
def test_the_reversal_script_restores_the_bytes_when_an_xattr_cannot_be_set(
    flac_fixture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the reversal script cannot set a live file's xattr [then] it still restores the audio, warns and leaves no temp file, [else stop].

    MUTATION TARGET: drop the ``except OSError`` around ``os.setxattr`` in the
    generated script's ``keep_metadata``.
    """
    if sys.platform == "darwin" or not _user_xattrs_supported(flac_fixture):
        pytest.skip("needs the Linux xattr path with user xattrs to refuse")
    monkeypatch.setattr(wt, "BACKUP_ROOT", tmp_path / "tb")
    monkeypatch.setattr(wt, "REVERSAL_ROOT", tmp_path / "rv")
    monkeypatch.setattr(wt, "FILE_BACKUP_ROOT", tmp_path / "fb")
    original = flac_fixture.read_bytes()
    delta = wt.TagDelta(
        path=flac_fixture, stable_id="rev-refuse",
        old=wt._read_current_tags(flac_fixture), new=wt._build_new_tags(_rec("rev-refuse")),
    )
    s = wt.apply_writes([delta], live=True, bulk=False)
    assert flac_fixture.read_bytes() != original
    _OS.setxattr(flac_fixture, "user.opendj.tag", b"kept")
    driver = (
        "import os, runpy, sys\n"
        "def refuse(*_a, **_k):\n"
        "    raise PermissionError(1, 'Operation not permitted')\n"
        "os.setxattr = refuse\n"
        f"sys.exit(runpy.run_path({str(s.reversal_scripts[0])!r}, run_name='reversal')['main']())\n"
    )
    r = subprocess.run([sys.executable, "-c", driver], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert "user.opendj.tag not kept" in r.stderr
    assert flac_fixture.read_bytes() == original
    assert not list(flac_fixture.parent.glob(f".{flac_fixture.name}.restore-*"))
