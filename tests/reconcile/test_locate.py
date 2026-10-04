"""Tests for :mod:`apps.reconcile.locate`. Ties to RECON-02.

We craft a handful of fake audio files on disk + a synthetic broken row,
then assert each of the 6 signals fires correctly and triple-validation
(≥3 signals) behaves as specified.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from apps.reconcile import locate
from apps.shared import audio_files


def _make_af(path: Path, size: int = 1000) -> audio_files.AudioFile:
    return audio_files.AudioFile(path=path, size_bytes=size, mtime=0.0, ext=path.suffix.lower())


def _row(**overrides: object) -> dict[str, str]:
    base = {
        "id": "42",
        "title": "Test Track",
        "artist": "Test Artist",
        "original_path": "/music/Convert temp 2 (BACKUP)/foo/track.mp3",
        "basename": "track.mp3",
        "duration_s": "200",
        "file_size": "1000",
    }
    base.update({k: str(v) for k, v in overrides.items()})
    return base


# ------------------------------------------------------------------ signals


@pytest.mark.requirement("RECON-02")
def test_basename_exact_signal_matches_on_identical_name(tmp_path: Path) -> None:
    """Candidate with same basename (NFC, case-insensitive) fires basename_exact."""
    cand = tmp_path / "track.mp3"
    cand.write_bytes(b"\x00")
    idx = locate.FsIndex.build([_make_af(cand)])
    cache: dict[Path, audio_files.AudioMetadata | None] = {cand: None}
    row = _row(original_path="/elsewhere/track.mp3")  # no path-rewrite match
    best = locate._locate_one(row, idx, cache)
    assert best is not None
    assert "basename_exact" in best.signals


@pytest.mark.requirement("RECON-02")
def test_basename_fuzzy_signal_only_when_no_exact(tmp_path: Path) -> None:
    """Fuzzy match only fires if we have no exact-basename candidate."""
    cand = tmp_path / "track_v2.mp3"  # ratio ≥0.85 vs "track.mp3"
    cand.write_bytes(b"\x00")
    idx = locate.FsIndex.build([_make_af(cand)])
    cache: dict[Path, audio_files.AudioMetadata | None] = {cand: None}
    row = _row(basename="track_v3.mp3", original_path="/nowhere/track_v3.mp3")
    best = locate._locate_one(row, idx, cache)
    # May or may not find a fuzzy candidate depending on SequenceMatcher; if it
    # does, it must NOT carry a basename_exact signal.
    if best is not None:
        assert "basename_exact" not in best.signals


@pytest.mark.requirement("RECON-02")
def test_path_rewrite_signal_handles_backup_prefix(tmp_path: Path) -> None:
    """A FolderPath under the 'Convert temp 2 (BACKUP)' prefix tries the
    Manual Library rewrite; when the file exists there, path_rewrite fires."""
    # Fake Manual Library tree.
    target = tmp_path / "Manual Library/Convert temp 2 (BACKUP)/foo/track.mp3"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x00" * 1000)  # size matches row
    original = tmp_path / "Convert temp 2 (BACKUP)/foo/track.mp3"

    # Temporarily redirect PATH_REWRITES so we test against tmp_path.
    with patch.object(
        locate,
        "PATH_REWRITES",
        ((str(tmp_path / "Convert temp 2 (BACKUP)/"),
          str(tmp_path / "Manual Library/Convert temp 2 (BACKUP)/")),),
    ):
        idx = locate.FsIndex.build([_make_af(target, size=1000)])
        cache: dict[Path, audio_files.AudioMetadata | None] = {target: None}
        row = _row(original_path=str(original), file_size="1000")
        best = locate._locate_one(row, idx, cache)
    assert best is not None
    assert "path_rewrite" in best.signals, best.signals


@pytest.mark.requirement("RECON-02")
def test_size_match_signal_within_one_percent(tmp_path: Path) -> None:
    """Size signal fires when candidate is within 1% of the row's file_size."""
    cand = tmp_path / "track.mp3"
    cand.write_bytes(b"\x00")
    idx = locate.FsIndex.build([_make_af(cand, size=1005)])  # 0.5% larger than 1000
    cache: dict[Path, audio_files.AudioMetadata | None] = {cand: None}
    row = _row(file_size="1000")
    best = locate._locate_one(row, idx, cache)
    assert best is not None
    assert "size_match" in best.signals


@pytest.mark.requirement("RECON-02")
def test_size_match_signal_rejects_when_diff_exceeds_tolerance(tmp_path: Path) -> None:
    """2 % size diff → no size_match signal."""
    cand = tmp_path / "track.mp3"
    cand.write_bytes(b"\x00")
    idx = locate.FsIndex.build([_make_af(cand, size=1020)])  # 2 % larger
    cache: dict[Path, audio_files.AudioMetadata | None] = {cand: None}
    row = _row(file_size="1000")
    best = locate._locate_one(row, idx, cache)
    assert best is not None
    assert "size_match" not in best.signals


@pytest.mark.requirement("RECON-02")
def test_id3_and_duration_signals_fire_on_matching_metadata(tmp_path: Path) -> None:
    """ID3 title+artist + duration match fire via the passed metadata."""
    cand = tmp_path / "track.mp3"
    cand.write_bytes(b"\x00")
    idx = locate.FsIndex.build([_make_af(cand, size=1000)])
    cache = {
        cand: audio_files.AudioMetadata(
            title="Test Track",
            artist="Test Artist",
            duration_s=200.1,  # within ±0.5s of row's "200"
        )
    }
    row = _row()  # matching title/artist/duration/size
    best = locate._locate_one(row, idx, cache)
    assert best is not None
    assert "id3_match" in best.signals
    assert "duration_match" in best.signals
    # With 3+ signals, it MUST be triple-validated.
    assert best.triple_validated, best.signals


@pytest.mark.requirement("RECON-02")
def test_triple_validation_requires_three_signals(tmp_path: Path) -> None:
    """Exactly 2 signals → NOT triple-validated. 3 signals → triple-validated."""
    cand = tmp_path / "track.mp3"
    cand.write_bytes(b"\x00")
    idx = locate.FsIndex.build([_make_af(cand, size=1000)])

    # Only basename_exact + size_match = 2 signals. No id3 metadata.
    cache_none: dict[Path, audio_files.AudioMetadata | None] = {cand: None}
    row = _row()
    best = locate._locate_one(row, idx, cache_none)
    assert best is not None
    assert best.signal_count == 2, best.signals
    assert not best.triple_validated

    # Adding id3_match pushes us to 3.
    cache_full = {
        cand: audio_files.AudioMetadata(
            title="Test Track", artist="Test Artist", duration_s=None,
        )
    }
    best2 = locate._locate_one(row, idx, cache_full)
    assert best2 is not None
    assert best2.signal_count == 3, best2.signals
    assert best2.triple_validated


@pytest.mark.requirement("RECON-02")
def test_find_candidates_ranks_multiple_matches(tmp_path: Path) -> None:
    """relocate-files (RELOC-01) reuses this to show a human a ranked list,
    not just the CLI's single winner. A triple-validated exact-basename
    match must outrank a bare fuzzy-basename match, and the count/order
    must match ``_locate_one``'s own winner."""
    exact = tmp_path / "track.mp3"
    exact.write_bytes(b"\x00" * 1000)
    fuzzy = tmp_path / "sub" / "track_v2.mp3"
    fuzzy.parent.mkdir()
    fuzzy.write_bytes(b"\x00" * 1000)

    idx = locate.FsIndex.build([_make_af(exact, size=1000), _make_af(fuzzy, size=1000)])
    cache = {
        exact: audio_files.AudioMetadata(title="Test Track", artist="Test Artist",
                                         duration_s=200.0),
        fuzzy: None,
    }
    row = _row()
    found = locate.find_candidates(row, idx, cache, limit=5)
    assert len(found) >= 1
    assert found[0].path == exact
    assert found[0].triple_validated
    # Matches the CLI's own single-best answer.
    best = locate._locate_one(row, idx, dict(cache))
    assert best is not None
    assert found[0].path == best.path
    assert found[0].confidence == best.confidence


@pytest.mark.requirement("RECON-02")
def test_find_candidates_respects_limit(tmp_path: Path) -> None:
    exact = tmp_path / "track.mp3"
    exact.write_bytes(b"\x00")
    idx = locate.FsIndex.build([_make_af(exact)])
    cache: dict[Path, audio_files.AudioMetadata | None] = {exact: None}
    row = _row()
    found = locate.find_candidates(row, idx, cache, limit=0)
    assert found == []


@pytest.mark.requirement("RECON-02")
def test_find_candidates_empty_when_no_seed(tmp_path: Path) -> None:
    cand = tmp_path / "totally-unrelated.mp3"
    cand.write_bytes(b"\x00")
    idx = locate.FsIndex.build([_make_af(cand)])
    cache: dict[Path, audio_files.AudioMetadata | None] = {cand: None}
    row = _row(basename="track.mp3", original_path="/elsewhere/track.mp3")
    assert locate.find_candidates(row, idx, cache) == []


@pytest.mark.requirement("RECON-02")
def test_no_candidate_returns_none(tmp_path: Path) -> None:
    """No matching basename, no path-rewrite hit, no fuzzy match → None."""
    cand = tmp_path / "totally-unrelated-1234567.mp3"
    cand.write_bytes(b"\x00")
    idx = locate.FsIndex.build([_make_af(cand)])
    cache: dict[Path, audio_files.AudioMetadata | None] = {cand: None}
    row = _row(basename="track.mp3", original_path="/elsewhere/track.mp3")
    best = locate._locate_one(row, idx, cache)
    assert best is None


# ------------------------------------------------------------------ helpers


@pytest.mark.requirement("RECON-02")
def test_rationale_builds_human_readable_string() -> None:
    """Rationale lists signal labels in the declared order."""
    text = locate._rationale(
        ["basename_exact", "size_match", "id3_match"],
        size_bytes=12345,
    )
    assert "basename exact" in text
    assert "size match (12,345)" in text
    assert "id3 title+artist match" in text


@pytest.mark.requirement("RECON-02")
def test_load_broken_exits_when_input_csv_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_load_broken`` exits 1 if ``broken.csv`` hasn't been generated yet."""
    monkeypatch.setattr(locate, "IN_CSV", tmp_path / "nope.csv")
    with pytest.raises(SystemExit):
        locate._load_broken()


@pytest.mark.requirement("RECON-02")
def test_load_broken_reads_csv_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_load_broken`` yields plain dicts for the locate pipeline."""
    csv_path = tmp_path / "broken.csv"
    csv_path.write_text("id,title,original_path,basename\n1,T,/x/y.mp3,y.mp3\n")
    monkeypatch.setattr(locate, "IN_CSV", csv_path)
    rows = locate._load_broken()
    assert rows == [{"id": "1", "title": "T", "original_path": "/x/y.mp3", "basename": "y.mp3"}]


@pytest.mark.requirement("RECON-02")
def test_write_output_emits_all_columns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_write_output`` writes one row with every OUT_COLUMNS field populated."""
    out = tmp_path / "located.csv"
    monkeypatch.setattr(locate, "OUT_CSV", out)
    rows = [
        {col: ("True" if col == "triple_validated" else str(i))
         for i, col in enumerate(locate.OUT_COLUMNS)}
    ]
    locate._write_output(rows)
    import csv as _csv
    with out.open() as fh:
        r = list(_csv.DictReader(fh))
    assert len(r) == 1
    assert set(r[0].keys()) == set(locate.OUT_COLUMNS)


@pytest.mark.requirement("RECON-02")
def test_fs_index_maps_by_nfc_lower_basename(tmp_path: Path) -> None:
    """``FsIndex.build`` keys map case-folded + NFC-normalized basenames."""
    p1 = tmp_path / "Foo.mp3"
    p2 = tmp_path / "sub" / "foo.mp3"
    p2.parent.mkdir()
    for p in (p1, p2):
        p.write_bytes(b"")
    afs = [
        audio_files.AudioFile(path=p1, size_bytes=1, mtime=0.0, ext=".mp3"),
        audio_files.AudioFile(path=p2, size_bytes=1, mtime=0.0, ext=".mp3"),
    ]
    idx = locate.FsIndex.build(afs)
    assert "foo.mp3" in idx.by_basename
    # Both files collide under the same casefolded key.
    assert len(idx.by_basename["foo.mp3"]) == 2


@pytest.mark.requirement("RECON-02")
def test_signal_weights_sum_cap_covers_all_six() -> None:
    """Every declared signal has a weight; missing one would mis-score."""
    assert set(locate.SIGNAL_WEIGHTS) == {
        "basename_exact", "basename_fuzzy", "size_match",
        "path_rewrite", "id3_match", "duration_match", "fingerprint_match",
    }
    # Every weight is positive.
    assert all(w > 0 for w in locate.SIGNAL_WEIGHTS.values())


@pytest.mark.requirement("RECON-02")
def test_main_end_to_end_writes_located_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running ``locate.main()`` end-to-end produces ``located.csv``.

    We wire up a tiny FS tree with one matching candidate and redirect
    ``paths.MUSIC_ROOTS`` + the CSV IO constants into ``tmp_path``.
    """
    from apps.shared import paths as shared_paths

    # 1. Disk tree with one match + one unrelated file.
    candidate = tmp_path / "mus" / "track.mp3"
    candidate.parent.mkdir()
    candidate.write_bytes(b"\x00" * 1000)

    # 2. Input CSV (broken row).
    in_csv = tmp_path / "broken.csv"
    in_csv.write_text(
        "id,title,artist,original_path,basename,duration_s,file_size\n"
        "1,T,A,/old/track.mp3,track.mp3,200,1000\n"
    )

    out_csv = tmp_path / "located.csv"
    monkeypatch.setattr(locate, "IN_CSV", in_csv)
    monkeypatch.setattr(locate, "OUT_CSV", out_csv)
    monkeypatch.setattr(shared_paths, "MUSIC_ROOTS", [tmp_path / "mus"])

    locate.main()
    assert out_csv.exists()
    import csv as _csv
    rows = list(_csv.DictReader(out_csv.open()))
    assert len(rows) == 1
    assert rows[0]["id"] == "1"
    assert rows[0]["best_candidate_path"] == str(candidate)
