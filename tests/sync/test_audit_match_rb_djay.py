"""Smoke tests for the audit CLI ``apps.audit.match_rb_djay`` (SYNC-02)."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.audit import match_rb_djay
from apps.shared.djay_db import DjayTrack


pytestmark = pytest.mark.requirement("SYNC-02")


@dataclass(slots=True)
class _FakeRB:
    id: str
    title: str
    artist: str
    file_path: Path | None = None
    duration_s: float | None = None
    isrc: str = ""


def _dj(uuid: str, **kwargs) -> DjayTrack:
    kwargs.setdefault("title", "")
    kwargs.setdefault("artist", "")
    kwargs.setdefault("isrc", "")
    kwargs.setdefault("source_uri", "")
    kwargs.setdefault("file_path", None)
    kwargs.setdefault("is_local", kwargs["file_path"] is not None)
    kwargs.setdefault("rating", 0)
    kwargs.setdefault("duration_s", None)
    kwargs.setdefault("play_count", 0)
    kwargs.setdefault("color_index", None)
    return DjayTrack(uuid=uuid, **kwargs)


def test_run_writes_csv_with_expected_columns(tmp_path: Path) -> None:
    rb = _FakeRB(
        id="rb-1",
        title="Song",
        artist="A",
        isrc="USA111",
        file_path=tmp_path / "Song.mp3",
        duration_s=180.0,
    )
    rb.file_path.touch()
    dj = _dj(
        uuid="u-1",
        title="Song",
        artist="A",
        isrc="USA111",
        file_path=tmp_path / "Song.mp3",
        duration_s=180.0,
    )
    out_dir = tmp_path / "out"
    result = match_rb_djay.run(
        out_dir=out_dir,
        use_fingerprint=False,
        rb_tracks=[rb],
        dj_tracks=[dj],
    )
    csv_path = out_dir / "matches.csv"
    assert csv_path.exists()
    with csv_path.open(newline="", encoding="utf-8") as fp:
        rows = list(csv.reader(fp))
    header = rows[0]
    # Contract with Phase 3: must include rb_id, djay_uuid, confidence,
    # signals_fired columns.
    for col in ("rb_id", "djay_uuid", "confidence", "signals_fired"):
        assert col in header
    assert result.stats["rb_total"] == 1
    assert result.stats["dj_total"] == 1
    assert len(result.matched) == 1


def test_run_with_no_tracks_produces_empty_csv(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    result = match_rb_djay.run(
        out_dir=out_dir, use_fingerprint=False, rb_tracks=[], dj_tracks=[]
    )
    csv_path = out_dir / "matches.csv"
    assert csv_path.exists()
    with csv_path.open(newline="", encoding="utf-8") as fp:
        rows = list(csv.reader(fp))
    # header + zero body rows
    assert len(rows) == 1
    assert result.stats["rb_total"] == 0


def test_no_fingerprint_flag_skips_cache_init(tmp_path: Path) -> None:
    """`--no-fingerprint` should not attempt to create the sqlite cache."""
    out_dir = tmp_path / "out"
    match_rb_djay.run(
        out_dir=out_dir, use_fingerprint=False, rb_tracks=[], dj_tracks=[]
    )
    assert not (out_dir / "fingerprints.sqlite").exists()
