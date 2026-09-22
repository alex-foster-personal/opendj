"""Regression for Codex P06-F01 / META-02.

Live tag writes must be atomic + recoverable: a mid-write failure must
leave the audio file byte-identical to its pre-write state. Prior to the
fix, ``_write_tags`` mutated the file in place via mutagen and a later
``_read_current_tags`` / verify failure left the file partially rewritten
with no way back.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.analysis import write_tags as wt
from apps.analysis.record import AnalysisRecord

pytestmark = pytest.mark.requirement("META-02")


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
    """If mutagen itself raises, the original file must be untouched."""
    monkeypatch.setattr(wt, "BACKUP_ROOT", tmp_path / "tb")
    monkeypatch.setattr(wt, "REVERSAL_ROOT", tmp_path / "rv")
    monkeypatch.setattr(wt, "FILE_BACKUP_ROOT", tmp_path / "fb")

    pre_digest = _sha(flac_fixture)

    def explode(_path: Path, _new: dict[str, str]) -> None:
        raise RuntimeError("synthetic mutagen failure")

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
