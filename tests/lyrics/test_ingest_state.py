"""ingest-state against a real state DB: matching, artifacts, honest counts.

Ported from the feature branch's ``tests/lyrics/test_ingest_state.py``, whose
assertions read word rows out of the retired ``lyric_word`` table. Under _V9
the words live in the per-track ``karaoke_words`` artifact and the row records
only their sha256, so the same intent is asserted through
``karaoke_cache.parse_words``.

Local policy mode throughout (``use_local_mode``): ingest of a bench run is
the same write on a laptop with no credentials, and nothing here may touch the
network.

- if --match-by file-path stops ingesting a manifest entry whose file_path is
  held by exactly one tracks row then broken
- if file-path matching ever picks a row when zero or several rows hold the
  path (or the entry has no file_path) then broken -- unmatched must be
  REPORTED with a reason, never guessed and never silently dropped
- if the default vendor-id matching stops working unchanged then broken
- if re-ingesting a LIVE track needs resurrect=True then every second batch
  run fails; if re-ingesting a TOMBSTONED track succeeds then the licensing
  purge is undone by the next batch -- both broken
- if a per-track failure aborts the run, or exits 0, then a 200-track batch
  either loses 199 good tracks or hides a bad one -- broken
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from apps.lyrics import ingest_state, karaoke_cache, store
from apps.lyrics.karaoke_cache import PIPELINE_VERSION
from apps.shared.state import db as state_db

from .conftest import use_local_mode

NOW = "2026-09-01T00:00:00+00:00"


def _track_entry(name: str, file_path: str | None = None, **overrides) -> dict:
    entry: dict = {
        "name": name,
        "artist": "A",
        "title": "T",
        "language_iso": "eng",
        "n_words": 2,
        "stats": {"source_method": "lrclib get", "pct_witness_red": 0.5},
        "words": [
            {"word": "one", "start_s": 1.0, "end_s": 1.2, "score": -0.5,
             "witness": "agree", "line_final": False},
            {"word": "two", "start_s": 1.5, "end_s": 1.9, "score": -0.4,
             "witness": "contradict", "line_final": True},
        ],
    }
    if file_path is not None:
        entry["file_path"] = file_path
    entry.update(overrides)
    return entry


@pytest.fixture
def db_path(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A migrated state DB at the canonical ``<data_dir>/state/state.db``.

    The path matters: ``sync_stamp.data_dir_for_connection`` derives the data
    dir (and therefore the machine identity and the karaoke cache location)
    from it, so a DB dumped straight into tmp_path would write its artifacts
    somewhere else entirely.
    """
    use_local_mode(monkeypatch)
    path = data_dir / "state" / "state.db"
    conn = state_db.open_rw(path)
    try:
        for stable_id, file_path in (
            ("sid-good", "/lib/good.mp3"),
            ("sid-dup-a", "/lib/dup.mp3"),
            ("sid-dup-b", "/lib/dup.mp3"),
            ("sid-vendor", "/lib/vendor.mp3"),
        ):
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, "
                "created_at, updated_at) VALUES (?, 'inferred', 'T', ?, ?, ?)",
                (stable_id, file_path, NOW, NOW),
            )
        conn.execute(
            "INSERT INTO track_vendor_ids (vendor, vendor_id, stable_id) "
            "VALUES ('rekordbox', '12345', 'sid-vendor')"
        )
    finally:
        conn.close()
    return path


def _write_run(tmp_path: Path, tracks: list[dict]) -> tuple[Path, Path]:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"tracks": tracks}), encoding="utf-8")
    coverage = tmp_path / "vocal-presence.json"
    coverage.write_text(
        json.dumps([{"track_id": t["name"], "coverage_pct": 80.0} for t in tracks]),
        encoding="utf-8",
    )
    return manifest, coverage


def _ingest(
    manifest: Path,
    coverage: Path | None,
    db_path: Path,
    *,
    write: bool = True,
    match_by: str = "vendor-id",
) -> ingest_state.IngestReport:
    return ingest_state.ingest_state(
        manifest=manifest,
        coverage=coverage,
        write=write,
        match_by=match_by,
        db_path=db_path,
        s3=None,
        cfg=None,
    )


def _ingested_ids(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT stable_id FROM lyric_verdict WHERE deleted_at IS NULL"
            )
        }
    finally:
        conn.close()


#-----------------------------------------------------------------------------
# matching
#-----------------------------------------------------------------------------
def test_file_path_match_ingests_the_unique_row(
    tmp_path: Path, db_path: Path, data_dir: Path
) -> None:
    report = _ingest(
        *_write_run(tmp_path, [_track_entry("crate000", "/lib/good.mp3")]),
        db_path,
        match_by="file-path",
    )
    assert (report.n_tracks, report.n_matched, report.n_ingested) == (1, 1, 1)
    assert report.failures == () and report.unmatched == ()
    assert _ingested_ids(db_path) == {"sid-good"}
    parsed = karaoke_cache.parse_words(
        karaoke_cache.cache_path(data_dir, "sid-good"), "sid-good"
    )
    assert [entry.word for entry in parsed.words] == ["one", "two"]
    assert report.n_words == 2


def test_file_path_match_never_guesses_and_names_every_reason(
    tmp_path: Path, db_path: Path
) -> None:
    report = _ingest(
        *_write_run(
            tmp_path,
            [
                _track_entry("crate001", "/lib/dup.mp3"),     # two rows hold this
                _track_entry("crate002", "/lib/absent.mp3"),  # no row holds this
                _track_entry("crate003"),                     # no file_path at all
            ],
        ),
        db_path,
        match_by="file-path",
    )
    assert _ingested_ids(db_path) == set(), "ambiguous/absent must stay unmatched"
    assert (report.n_tracks, report.n_matched, report.n_ingested) == (3, 0, 0)
    assert len(report.unmatched) == 3, "unmatched is reported, never dropped"
    reasons = " | ".join(report.unmatched)
    assert "crate001" in reasons and "2 rows hold this file_path" in reasons
    assert "crate002" in reasons and "0 rows hold this file_path" in reasons
    assert "crate003" in reasons and "no file_path key in manifest entry" in reasons


def test_vendor_id_default_unchanged(tmp_path: Path, db_path: Path) -> None:
    manifest, coverage = _write_run(tmp_path, [_track_entry("12345")])
    report = ingest_state.ingest_state(
        manifest=manifest,
        coverage=coverage,
        write=True,
        match_by="vendor-id",
        db_path=db_path,
        s3=None,
        cfg=None,
    )
    assert report.n_ingested == 1
    assert _ingested_ids(db_path) == {"sid-vendor"}


def test_an_unknown_vendor_id_is_unmatched_not_invented(
    tmp_path: Path, db_path: Path
) -> None:
    report = _ingest(*_write_run(tmp_path, [_track_entry("99999")]), db_path)
    assert _ingested_ids(db_path) == set()
    assert len(report.unmatched) == 1
    assert "no rekordbox vendor id in state.db" in report.unmatched[0]


def test_an_unknown_match_mode_is_refused_by_name(
    tmp_path: Path, db_path: Path
) -> None:
    manifest, coverage = _write_run(tmp_path, [_track_entry("12345")])
    with pytest.raises(ValueError, match="unhandled match_by 'isrc'"):
        _ingest(manifest, coverage, db_path, match_by="isrc")


#-----------------------------------------------------------------------------
# what a matched track writes
#-----------------------------------------------------------------------------
def test_the_row_records_the_artifact_and_its_provenance(
    tmp_path: Path, db_path: Path, data_dir: Path
) -> None:
    _ingest(*_write_run(tmp_path, [_track_entry("12345")]), db_path)
    conn = state_db.open_rw(db_path)
    try:
        verdict = store.get_verdict(conn, "sid-vendor")
    finally:
        conn.close()
    assert verdict is not None
    assert verdict.verdict == "vocal", "80% stem vocal coverage bands as vocal"
    assert verdict.coverage_pct == 80.0
    assert verdict.source == "lrclib get"
    assert verdict.language_iso3 == "eng"
    assert (verdict.n_words, verdict.n_lines) == (2, 1)
    assert verdict.pct_witness_red == 0.5
    assert verdict.pipeline_version == PIPELINE_VERSION
    path = karaoke_cache.cache_path(data_dir, "sid-vendor")
    assert path.is_file()
    assert verdict.words_content_hash == karaoke_cache.write_words(
        path, karaoke_cache.parse_words(path, "sid-vendor")
    )[1]


def test_without_coverage_the_verdict_is_unknown_not_guessed(
    tmp_path: Path, db_path: Path
) -> None:
    manifest, _ = _write_run(tmp_path, [_track_entry("12345")])
    _ingest(manifest, None, db_path)
    conn = state_db.open_rw(db_path)
    try:
        verdict = store.get_verdict(conn, "sid-vendor")
    finally:
        conn.close()
    assert verdict is not None
    assert verdict.verdict == "unknown"
    assert verdict.coverage_pct is None


def test_write_false_matches_and_reports_but_writes_nothing(
    tmp_path: Path, db_path: Path, data_dir: Path
) -> None:
    manifest, coverage = _write_run(tmp_path, [_track_entry("12345")])
    report = _ingest(manifest, coverage, db_path, write=False)
    assert (report.n_matched, report.n_ingested, report.n_words) == (1, 0, 0)
    assert _ingested_ids(db_path) == set()
    assert not karaoke_cache.cache_dir(data_dir).exists()
    rendered = ingest_state.format_report(report, manifest=manifest, write=False)
    assert "[DRY-RUN]" in rendered
    assert "re-run with --write" in rendered
    assert f"denominator: 1 tracks in {manifest.name}" in rendered


#-----------------------------------------------------------------------------
# re-ingest: upsert on a live row, refusal on a tombstoned one
#-----------------------------------------------------------------------------
def test_ingesting_the_same_track_twice_is_a_plain_upsert(
    tmp_path: Path, db_path: Path
) -> None:
    """resurrect=False only bites a TOMBSTONED row.

    A live row is simply updated, which is what makes a re-run of a 200-track
    batch safe. Asserted explicitly because the opposite reading (every
    re-ingest needs resurrect=True) would make the batch driver unusable.
    """
    manifest, coverage = _write_run(tmp_path, [_track_entry("12345")])
    first = _ingest(manifest, coverage, db_path)
    second = _ingest(manifest, coverage, db_path)

    assert first.n_ingested == second.n_ingested == 1
    assert second.failures == ()
    assert _ingested_ids(db_path) == {"sid-vendor"}
    conn = state_db.open_rw(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM lyric_verdict").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM local_changelog WHERE table_name='lyric_verdict'"
        ).fetchone()[0] == 2, "each upsert is its own pushable changelog entry"
    finally:
        conn.close()


def test_a_purged_track_is_not_resurrected_by_the_next_batch(
    tmp_path: Path, db_path: Path
) -> None:
    manifest, coverage = _write_run(tmp_path, [_track_entry("12345")])
    _ingest(manifest, coverage, db_path)
    conn = state_db.open_rw(db_path)
    try:
        store.tombstone(conn, "sid-vendor")
    finally:
        conn.close()

    report = _ingest(manifest, coverage, db_path)

    assert report.n_matched == 1
    assert report.n_ingested == 0
    assert len(report.failures) == 1
    assert "refusing to resurrect" in report.failures[0]
    assert _ingested_ids(db_path) == set()


#-----------------------------------------------------------------------------
# per-track failures
#-----------------------------------------------------------------------------
def test_a_bad_track_neither_aborts_the_run_nor_exits_zero(
    tmp_path: Path, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bad entry claims a word count the artifact does not hold."""
    manifest, coverage = _write_run(
        tmp_path,
        [
            _track_entry("crate001", "/lib/vendor.mp3", n_words=99),
            _track_entry("crate000", "/lib/good.mp3"),
            _track_entry("crate002", "/lib/absent.mp3"),
        ],
    )
    code = ingest_state.main(
        manifest=manifest,
        coverage=coverage,
        write=True,
        match_by="file-path",
        db_path=db_path,
        s3=None,
        cfg=None,
    )
    printed = capsys.readouterr().out
    assert code == 1, "a silent 0 lets a batch script conclude the job is done"
    assert _ingested_ids(db_path) == {"sid-good"}, "the good track still landed"
    assert "[ERROR] 1 track(s) failed to ingest:" in printed
    assert "manifest claims n_words=99" in printed
    assert "[WARN] 1 track(s) could not be matched" in printed


def test_a_track_with_no_source_method_is_a_named_failure(
    tmp_path: Path, db_path: Path
) -> None:
    """Without a source the purge lever could never find these words again."""
    entry = _track_entry("12345")
    entry["stats"] = {"pct_witness_red": 0.5}
    report = _ingest(*_write_run(tmp_path, [entry]), db_path)
    assert report.n_ingested == 0
    assert len(report.failures) == 1
    assert "no stats.source_method" in report.failures[0]
    assert _ingested_ids(db_path) == set()
