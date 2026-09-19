"""Tests for apps.spotify.reporting."""
from __future__ import annotations

import csv
from datetime import UTC
from pathlib import Path

import pytest

from apps.spotify.acquisition import build_acquisition_entries
from apps.spotify.client import SpotifyPlaylist, SpotifyTrack
from apps.spotify.matcher_adapter import LocalTrack, MatchedPair, MatchResult
from apps.spotify.reporting import (
    build_report_dir,
    write_all_reports,
    write_matches_csv,
    write_to_acquire_csv,
)


def _playlist() -> SpotifyPlaylist:
    return SpotifyPlaylist(
        id="pl123", name="Pop 2026", snapshot_id="snap-1",
        owner="dev3", description="", tracks=(),
    )


def _src(sid, title="Title", isrc="USABC2500001") -> SpotifyTrack:
    return SpotifyTrack(
        spotify_id=sid, spotify_uri=f"spotify:track:{sid}", isrc=isrc,
        title=title, artists=("Artist",), album="Album",
        duration_ms=200000, is_local=False,
    )


@pytest.mark.requirement("CAT-01")
def test_build_report_dir_uses_timestamp(tmp_path: Path) -> None:
    from datetime import datetime
    ts = datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)
    paths = build_report_dir("pl123", root=tmp_path, timestamp=ts)
    assert paths.root.name == "import-pl123-20260417T120000Z"


@pytest.mark.requirement("CAT-01")
def test_matches_csv_roundtrips(tmp_path: Path) -> None:
    src1 = _src("t1", title="Hello", isrc="USABC2500001")
    src2 = _src("t2", title="Mystery, with comma", isrc=None)
    tgt = LocalTrack("s1", "USABC2500001", "Hello", ("Artist",), 200000)
    pairs = [
        MatchedPair(src1, tgt, 0.35, ("isrc",), "matched"),
        MatchedPair(src2, None, 0.0, (), "unmatched"),
    ]
    out = tmp_path / "m.csv"
    write_matches_csv(pairs, out)
    rows = list(csv.DictReader(out.read_text().splitlines()))
    assert len(rows) == 2
    assert rows[0]["status"] == "matched"
    assert rows[0]["local_stable_id"] == "s1"
    assert rows[1]["title"] == "Mystery, with comma"


@pytest.mark.requirement("CAT-01")
def test_to_acquire_csv_has_all_5_source_cols(tmp_path: Path) -> None:
    playlist = _playlist()
    pairs = [MatchedPair(_src("t1"), None, 0.0, (), "unmatched")]
    entries = build_acquisition_entries(pairs)
    out = tmp_path / "ta.csv"
    write_to_acquire_csv(playlist, entries, out)
    rows = list(csv.DictReader(out.read_text().splitlines()))
    assert len(rows) == 1
    for col in ("suggested_source_beatport", "suggested_source_bandcamp",
                "suggested_source_qobuz", "suggested_source_apple",
                "suggested_source_discogs"):
        assert rows[0][col].startswith("https://"), col


@pytest.mark.requirement("CAT-01")
def test_write_all_reports_produces_four_files(tmp_path: Path) -> None:
    playlist = _playlist()
    src = _src("t1", title="Hello", isrc="USABC2500001")
    pair = MatchedPair(
        src, LocalTrack("s1", "USABC2500001", "Hello", ("Artist",), 200000),
        0.35, ("isrc",), "matched",
    )
    paths = build_report_dir("pl123", root=tmp_path)
    write_all_reports(playlist, MatchResult(pairs=[pair]), paths,
                      include_timestamp=False, runtime_seconds=0.1)
    for p in (paths.matches_csv, paths.to_acquire_csv,
              paths.to_acquire_md, paths.summary_md):
        assert p.exists()
    assert "Match rate: 100.0%" in paths.summary_md.read_text()
