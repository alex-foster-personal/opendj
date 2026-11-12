"""FWATCH-02 overlap, rewrite, and NATIVE-30 tests for folder rescan.

Continuation of ``test_folder_rescan.py``, split because that file crossed the
1500-line ``file_size`` ratchet (issue #2540). Contract remains
``apps/shared/state/ingest/folder_rescan.py``. Nothing was cut.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from apps.shared.state.ingest import folder_rescan
from tests.shared.state.folder_rescan_other_source_helpers import (
    _refresh_untouched_bytes,
    _rewrite_three_analyzed_files,
    _seed_analysis,
    _serving,
)

from .test_folder_rescan import _live_tracks, _write_wav, _write_wav_sample, _writer


@pytest.mark.requirement("NATIVE-30")
def test_the_same_hash_on_refresh_keeps_analysis(state_conn, state_db_path: Path, tmp_path: Path) -> None:
    """[if] a refresh writes the same hash [then] the analysis stays, [else stop]."""
    from datetime import UTC, datetime

    from apps.analysis.lanes import LaneResult
    from apps.analysis.record import AnalysisRecord
    from apps.analysis.store import upsert_record

    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav_sample(audio_path, 0)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    stable_id = _live_tracks(state_conn)[0][0]
    upsert_record(
        AnalysisRecord(
            stable_id=stable_id,
            backend="own_beatgrid.backfill",
            backend_version="1.6.0",
            analyzed_at=datetime(2026, 10, 8, tzinfo=UTC),
            duration_s=1.0,
            sample_rate=44100,
            bpm=120.0,
            bpm_confidence=0.9,
            key_camelot="8A",
            key_openkey="1m",
            key_confidence=0.8,
            energy=5,
            producer="backfill",
            producer_version="1.6.0",
            uses_model=False,
            decode_fingerprint="sha256:" + "66" * 32,
            lanes={"beatgrid": LaneResult(status="failed", reason="no_trackable_pulse", payload={})},
        ),
        db_path=state_db_path,
    )
    bumped = audio_path.stat().st_mtime + 5
    os.utime(audio_path, (bumped, bumped))
    folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)
    assert state_conn.execute("SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (stable_id,)).fetchone()[0] == 1


@pytest.mark.requirement("NATIVE-30")
def test_a_missing_prior_hash_keeps_analysis_made_after_the_file_was_written(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] a row with no stored hash is refreshed and its analysis postdates the file [then] it is kept, [else stop].

    Dropping on a missing hash alone erases every such row's analysis on the
    first rescan that walks its folder (Sol P1 on #5886).
    """
    kept = _refresh_untouched_bytes(state_conn, state_db_path, tmp_path, analyzed_after_write=True, stored_hash=None)
    assert kept == 1, "if a missing hash alone drops analysis newer than the file then broken"


@pytest.mark.requirement("NATIVE-30")
def test_control_a_missing_prior_hash_drops_analysis_older_than_the_file(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] the analysis predates the file's last write and no hash was stored [then] it is dropped, [else stop]."""
    kept = _refresh_untouched_bytes(state_conn, state_db_path, tmp_path, analyzed_after_write=False, stored_hash=None)
    assert kept == 0, "if analysis older than the file survives a hash-less refresh then broken"


@pytest.mark.requirement("NATIVE-30")
def test_a_missing_prior_hash_drops_analysis_of_bytes_replaced_with_the_old_mtime(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] another recording of the same length replaces the file and keeps its mtime [then] the analysis drops, [else stop].

    The mtime alone says the analysis is newer than the file; the inode
    change the copy made says otherwise (Sol P1 on #5886).
    """
    kept = _refresh_untouched_bytes(
        state_conn,
        state_db_path,
        tmp_path,
        analyzed_after_write=True,
        stored_hash=None,
        replaced_keeping_mtime=True,
    )
    assert kept == 0, "if a replacement that restored the old mtime keeps the old analysis then broken"


@pytest.mark.requirement("NATIVE-30")
def test_a_missing_prior_hash_with_a_zoneless_stamp_drops_analysis_however_new(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] no hash is stored and the analysis stamp has no zone [then] it drops even if newer, [else stop].

    A naive analyzed_at is unreadable, not UTC: read as UTC, a local-time
    stamp could pass the mtime rule and keep analysis of replaced bytes
    (Sol P1 on #5886). The aware twin above keeps the same analysis.
    """
    kept = _refresh_untouched_bytes(
        state_conn, state_db_path, tmp_path, analyzed_after_write=True, stored_hash=None, naive_stamp=True
    )
    assert kept == 0, "if a zoneless stamp is read as UTC and keeps analysis then broken"


@pytest.mark.requirement("NATIVE-30")
def test_a_missing_prior_hash_and_another_duration_drops_analysis_however_new(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] no hash is stored and the length differs [then] newer analysis drops, [else stop].

    A replacement that keeps or backdates the old mtime passes the mtime
    rule, so a second recording's length must not (Sol P1 on #5886).
    """
    kept = _refresh_untouched_bytes(
        state_conn,
        state_db_path,
        tmp_path,
        analyzed_after_write=True,
        stored_hash=None,
        stored_duration_shift_ms=5000,
    )
    assert kept == 0, "if a hash-less row of another length keeps its analysis then broken"


@pytest.mark.requirement("NATIVE-30")
def test_control_a_different_stored_hash_drops_analysis_however_new(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] a stored hash differs from the file's [then] the analysis is dropped even if newer, [else stop].

    The overshoot of the mtime rule is to let it overrule a hash that proves
    the bytes changed.
    """
    kept = _refresh_untouched_bytes(
        state_conn, state_db_path, tmp_path, analyzed_after_write=True, stored_hash="sha256:" + "99" * 32
    )
    assert kept == 0, "if a newer timestamp overrules a different hash then broken"


@pytest.mark.requirement("NATIVE-30")
def test_a_rescan_over_the_analysis_drop_bound_keeps_the_excess_and_reports_it(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] drops exceed the bound [then] excess analysis is kept and counted, [else stop]."""
    report, records, moved = _rewrite_three_analyzed_files(state_conn, state_db_path, tmp_path, max_analysis_drops=1)
    assert sorted(records.values()) == [0, 1, 1], "if the excess rows lost analysis then broken"
    assert report.analysis_drops_withheld == 2
    assert report.warning is not None and "2 rewritten row(s) over the analysis drop bound of 1" in report.warning
    assert report.signature == "", "the withheld rows must be refreshed again next cycle"
    for sid, count in records.items():
        assert moved[sid] == (count == 0), "a withheld row keeps its stored hash"
    for sid in (sid for sid, count in records.items() if count):
        served, stale = _serving(state_conn, sid)
        assert served == 0, "if a withheld row still serves analysis of the old bytes then broken"
        assert stale > 0, "a withheld row's kept records are marked stale"


@pytest.mark.requirement("NATIVE-30")
def test_a_withheld_path_counts_every_row_that_shares_it(state_conn, state_db_path: Path, tmp_path: Path) -> None:
    """[if] a withheld path has two analyzed rows [then] both are counted and kept, [else stop]."""
    from datetime import UTC, datetime

    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav_sample(audio_path, 0)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    writer.upsert_track(
        stable_id="isrc-twin",
        stable_id_tier="isrc",
        title="Twin",
        artists=["A"],
        album=None,
        isrc="USRC17607839",
        duration_ms=1,
        file_path=str(audio_path),
        audio_hash="sha256:" + "11" * 32,
    )
    before = dict(state_conn.execute("SELECT stable_id, audio_hash FROM tracks").fetchall())
    for stable_id in before:
        _seed_analysis(state_db_path, stable_id, datetime(2026, 10, 8, tzinfo=UTC))
    _write_wav_sample(audio_path, 2000)
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    report = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature, max_analysis_drops=0)
    assert report.analysis_drops_withheld == 2, "if only the primary row is counted then broken"
    assert dict(state_conn.execute("SELECT stable_id, audio_hash FROM tracks").fetchall()) == before
    for stable_id in before:
        assert state_conn.execute("SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (stable_id,)).fetchone()[0] == 1


@pytest.mark.requirement("NATIVE-30")
def test_control_a_rescan_at_the_analysis_drop_bound_drops_every_analysis(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] drops are at the bound [then] every rewritten row loses analysis, [else stop]."""
    report, records, moved = _rewrite_three_analyzed_files(state_conn, state_db_path, tmp_path, max_analysis_drops=3)
    assert list(records.values()) == [0, 0, 0]
    assert all(moved.values())
    assert report.analysis_drops_withheld == 0
    assert report.warning is None


@pytest.mark.requirement("NATIVE-30")
def test_a_rewritten_row_with_no_analysis_does_not_spend_the_drop_budget(
    state_conn, state_db_path: Path, tmp_path: Path
) -> None:
    """[if] a rewritten row has no analysis [then] it refreshes without spending the drop bound, [else stop]."""
    from datetime import UTC, datetime

    root = tmp_path / "music"
    paths = [root / f"t{i}.wav" for i in range(3)]
    for path in paths:
        _write_wav_sample(path, 0)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    before = dict(state_conn.execute("SELECT stable_id, audio_hash FROM tracks").fetchall())
    analyzed = sorted(before)[0]
    _seed_analysis(state_db_path, analyzed, datetime(2026, 10, 8, tzinfo=UTC))
    for index, path in enumerate(paths, start=1):
        _write_wav_sample(path, 1000 * index)
        os.utime(path, (os.path.getmtime(path) + 5, os.path.getmtime(path) + 5))
    report = folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature, max_analysis_drops=0)
    after = dict(state_conn.execute("SELECT stable_id, audio_hash FROM tracks").fetchall())
    assert report.analysis_drops_withheld == 1, "only the analyzed row may be counted against the bound"
    for sid, hash_before in before.items():
        if sid == analyzed:
            assert after[sid] == hash_before, "control: the analyzed row over the bound is still withheld"
        else:
            assert after[sid] != hash_before, "if a row with no analysis is withheld for the bound then broken"


@pytest.mark.requirement("NATIVE-30")
def test_a_rewrite_keeps_a_fingerprint_content_hash(state_conn, tmp_path: Path) -> None:
    """[if] a rewrite adopts a fingerprint row [then] its content hash stays, [else stop]."""
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav(audio_path)
    writer = _writer(state_conn)
    digest = "sha256:" + "ab" * 32
    writer.upsert_track(
        stable_id="fp-kept",
        stable_id_tier="fingerprint",
        title="Kept",
        artists=["A"],
        album=None,
        isrc=None,
        duration_ms=1,
        file_path=str(audio_path),
        content_hash=digest,
        audio_hash="sha256:" + "11" * 32,
    )
    report = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    row = state_conn.execute(
        "SELECT stable_id, stable_id_tier, content_hash FROM tracks WHERE deleted_at IS NULL"
    ).fetchone()
    assert report.tracks_removed == 0
    assert row[0] == "fp-kept"
    assert row[1] == "fingerprint"
    assert row[2] == digest


@pytest.mark.requirement("NATIVE-30")
@pytest.mark.parametrize("stored_artists", ['{"name":"not-a-list"}', None, ""])
def test_a_non_list_artists_json_fails_the_sibling_refresh(
    state_conn, tmp_path: Path, stored_artists: str | None
) -> None:
    """[if] artists_json is not a list (NULL and "" included) [then] refresh raises and keeps it, [else stop]."""
    root = tmp_path / "music"
    audio_path = root / "a.wav"
    _write_wav_sample(audio_path, 0)
    writer = _writer(state_conn)
    first = folder_rescan.reconcile_folders(writer, [root], previous_signature="")
    inferred_id = _live_tracks(state_conn)[0][0]
    writer.upsert_track(
        stable_id="isrc-strong",
        stable_id_tier="isrc",
        title="Strong",
        artists=["A"],
        album=None,
        isrc="USRC17607839",
        duration_ms=1,
        file_path=str(audio_path),
        audio_hash="sha256:" + "11" * 32,
    )
    state_conn.execute(
        "UPDATE tracks SET artists_json = ? WHERE stable_id = ?",
        (stored_artists, inferred_id),
    )
    state_conn.commit()
    _write_wav_sample(audio_path, 2000)
    os.utime(audio_path, (os.path.getmtime(audio_path) + 5, os.path.getmtime(audio_path) + 5))
    with pytest.raises(ValueError, match="artists_json"):
        folder_rescan.reconcile_folders(writer, [root], previous_signature=first.signature)
    stored = state_conn.execute(
        "SELECT artists_json FROM tracks WHERE stable_id = ?",
        (inferred_id,),
    ).fetchone()[0]
    assert stored == stored_artists, "if a malformed value is replaced (by [] or anything) then broken"


@pytest.mark.requirement("NATIVE-30")
def test_a_folder_import_bounds_analysis_drops_like_a_rescan(state_conn, state_db_path: Path, tmp_path: Path) -> None:
    """[if] an import rewrites past the drop bound [then] the excess is kept unserved, [else stop]."""
    from datetime import UTC, datetime

    from apps.shared.state.ingest import folder

    root = tmp_path / "music"
    paths = [root / f"t{i:03}.wav" for i in range(folder.MAX_ANALYSIS_DROPS_PER_CYCLE + 1)]
    for path in paths:
        _write_wav_sample(path, 0)
    writer = _writer(state_conn)
    folder.ingest_folder(writer, [root], dry_run=False)
    ids = [sid for sid, _path in _live_tracks(state_conn)]
    for stable_id in ids:
        _seed_analysis(state_db_path, stable_id, datetime(2026, 10, 8, tzinfo=UTC))
    assert all(_serving(state_conn, sid)[0] for sid in ids), "the control: every seeded row is served"
    for path in paths:
        _write_wav_sample(path, 2000)
        os.utime(path, (os.path.getmtime(path) + 5, os.path.getmtime(path) + 5))
    report = folder.ingest_folder(writer, [root], dry_run=False)
    kept = [
        sid
        for sid in ids
        if state_conn.execute("SELECT COUNT(*) FROM analysis WHERE stable_id = ?", (sid,)).fetchone()[0]
    ]
    assert report.analysis_drops_withheld == 1, "if the import drops past the bound then broken"
    assert len(kept) == 1, "only the one row over the bound keeps its records"
    assert not any(_serving(state_conn, sid)[0] for sid in ids), "no row serves the old bytes' analysis"
