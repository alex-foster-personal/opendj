"""The one-shot conversion of the branch-era lyric tables into _V10 + artifacts.

The fixture reproduces what the D13.6 runbook leaves behind on the maintainer's real
machine: a DB that ran the karaoke FEATURE branch (whose own ``_V8`` created
``lyric_verdict`` + ``lyric_word``), had both tables renamed aside with their
indexes dropped, was stamped back to 7 and then walked up main's ladder to 9.
The legacy DDL below is the branch text at
``apps/shared/state/schema.py:508-546`` with the ``_legacy`` suffix applied
(``ALTER TABLE ... RENAME TO`` also rewrites the child's FK target, which is
why ``lyric_word_legacy`` points at ``lyric_verdict_legacy`` here). It is
transcribed rather than imported because the branch is not on this ladder: the
point is to migrate data written by code that no longer exists.

- if a converted row loses its original computed_at then every migrated track
  claims the pipeline judged it on migration day -- broken
- if pipeline_version is not the CURRENT constant then a hash cannot be traced
  to the writer that produced it (this writer produced the bytes) -- broken
- if the artifact on disk does not hash to words_content_hash then the row
  vouches for bytes that are not there -- broken
- if idx_lyric_verdict_red ends up attached to the LEGACY table then the new
  table ships without its triage index -- broken
- if the legacy tables are dropped while any row failed to convert then the
  data needed to finish the job is gone -- broken
- if --dry-run writes a row, a file or a changelog entry then the rehearsal is
  the performance -- broken
- if a human override is not carried over then the one value nothing can
  recompute is lost -- broken
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from apps.lyrics import karaoke_cache, legacy_words, store
from apps.lyrics.karaoke_cache import PIPELINE_VERSION
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema

from .conftest import use_local_mode

T_A = "2026-08-01T10:00:00+00:00"
T_B = "2026-08-02T11:00:00+00:00"
T_C = "2026-08-03T12:00:00+00:00"

#: The branch DDL, verbatim apart from the ``_legacy`` names.
_LEGACY_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS lyric_verdict_legacy (
        stable_id       TEXT PRIMARY KEY REFERENCES tracks(stable_id) ON DELETE CASCADE,
        verdict         TEXT NOT NULL CHECK (verdict IN
                          ('vocal','sparse','no-lyrics','unknown')),
        coverage_pct    REAL CHECK (coverage_pct IS NULL OR
                                    (coverage_pct >= 0 AND coverage_pct <= 100)),
        source          TEXT,
        language_iso3   TEXT,
        n_words         INTEGER,
        pct_witness_red REAL CHECK (pct_witness_red IS NULL OR
                                    (pct_witness_red >= 0 AND pct_witness_red <= 1)),
        override        TEXT CHECK (override IS NULL OR override IN
                          ('vocal','sparse','no-lyrics')),
        override_note   TEXT,
        computed_at     TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lyric_word_legacy (
        stable_id   TEXT NOT NULL REFERENCES lyric_verdict_legacy(stable_id) ON DELETE CASCADE,
        idx         INTEGER NOT NULL,
        word        TEXT NOT NULL,
        start_s     REAL,
        end_s       REAL,
        score       REAL,
        witness     TEXT CHECK (witness IS NULL OR witness IN
                      ('agree','drift','contradict','lost','unheard','unmatchable')),
        line_final  INTEGER NOT NULL DEFAULT 0 CHECK (line_final IN (0, 1)),
        PRIMARY KEY (stable_id, idx)
    )
    """,
)

#: ``(stable_id, verdict, coverage_pct, source, language_iso3, n_words,
#: pct_witness_red, override, override_note, computed_at)``.
_LEGACY_ROWS: tuple[tuple[object, ...], ...] = (
    ("sid-a", "vocal", 80.0, "musixmatch api", "eng", 2, 0.25, None, None, T_A),
    ("sid-b", "sparse", 20.0, "lrclib get", "fra", 3, 0.5, "no-lyrics", "heard it", T_B),
    ("sid-c", "no-lyrics", 1.0, "vocal-presence", None, 0, None, None, None, T_C),
)

#: ``(stable_id, idx, word, start_s, end_s, score, witness, line_final)``.
#: ``sid-c`` deliberately has none: a wordless verdict is a real state (the
#: track was judged instrumental, so nothing was ever aligned).
_LEGACY_WORDS: tuple[tuple[object, ...], ...] = (
    ("sid-a", 0, "one", 1.0, 1.2, -0.5, "agree", 0),
    ("sid-a", 1, "two", 1.5, 1.9, -0.4, "contradict", 1),
    ("sid-b", 0, "trois", 2.0, 2.2, -0.3, "drift", 0),
    ("sid-b", 1, "quatre", 2.3, 2.6, None, None, 1),
    ("sid-b", 2, "cinq", 3.0, 3.4, -0.1, "lost", 1),
)


#-----------------------------------------------------------------------------
# fixture: a DB in exactly the post-runbook state
#-----------------------------------------------------------------------------
def _apply_ladder_to_v8(conn: sqlite3.Connection) -> None:
    """Run the REAL ladder, stopping at 8.

    Rolling ``SCHEMA_VERSION`` back is the same trick
    ``tests/shared/state/test_schema_v10.py`` uses: hand-writing v8 here would
    test this file's transcription of main's schema rather than main's schema.
    """
    original = state_schema.SCHEMA_VERSION
    state_schema.SCHEMA_VERSION = 8
    try:
        state_schema.apply_migrations(conn)
    finally:
        state_schema.SCHEMA_VERSION = original


def _seed_legacy_db(db_path: Path, *, words: tuple[tuple[object, ...], ...]) -> None:
    """A v8 DB carrying the renamed-aside branch tables and their rows."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        _apply_ladder_to_v8(conn)
        for stable_id, *_ in _LEGACY_ROWS:
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, title, created_at, "
                "updated_at) VALUES (?, 'inferred', ?, ?, ?)",
                (stable_id, f"track {stable_id}", T_A, T_A),
            )
        for statement in _LEGACY_DDL:
            conn.execute(statement)
        for row in _LEGACY_ROWS:
            conn.execute(
                "INSERT INTO lyric_verdict_legacy(stable_id, verdict, coverage_pct, "
                "source, language_iso3, n_words, pct_witness_red, override, "
                "override_note, computed_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*row, T_A),
            )
        conn.executemany(
            "INSERT INTO lyric_word_legacy(stable_id, idx, word, start_s, end_s, "
            "score, witness, line_final) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            words,
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def legacy_conn(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """A v10 DB (ladder applied on open) still holding the legacy tables."""
    use_local_mode(monkeypatch)
    db_path = data_dir / "state" / "state.db"
    _seed_legacy_db(db_path, words=_LEGACY_WORDS)
    connection = state_db.open_rw(db_path)
    try:
        yield connection
    finally:
        connection.close()


def _migrate(
    conn: sqlite3.Connection, data_dir: Path, *, dry_run: bool = False
) -> legacy_words.LegacyMigrationReport:
    return legacy_words.migrate_legacy_words(
        conn, data_dir=data_dir, s3=None, cfg=None, dry_run=dry_run
    )


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


#-----------------------------------------------------------------------------
# the fixture's own assumptions
#-----------------------------------------------------------------------------
def test_the_ladder_reaches_v9_with_the_legacy_tables_still_present(
    legacy_conn: sqlite3.Connection,
) -> None:
    """If this fails, every other test in the file is testing nothing."""
    assert legacy_conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[
        0
    ] == state_schema.SCHEMA_VERSION
    assert {"lyric_verdict", "lyric_verdict_legacy", "lyric_word_legacy"} <= _tables(
        legacy_conn
    )
    assert legacy_conn.execute("SELECT COUNT(*) FROM lyric_verdict").fetchone()[0] == 0


def test_the_triage_index_lands_on_the_new_table_not_the_legacy_one(
    legacy_conn: sqlite3.Connection,
) -> None:
    """The bare CREATE INDEX in _V10 only works because the runbook dropped the
    branch-era index; this asserts where the surviving one is attached."""
    rows = legacy_conn.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND "
        "name = 'idx_lyric_verdict_red'"
    ).fetchall()
    assert [(str(name), str(table)) for name, table in rows] == [
        ("idx_lyric_verdict_red", "lyric_verdict")
    ]


#-----------------------------------------------------------------------------
# the conversion
#-----------------------------------------------------------------------------
def test_every_row_converts_to_a_sixteen_column_row(
    legacy_conn: sqlite3.Connection, data_dir: Path
) -> None:
    report = _migrate(legacy_conn, data_dir)

    assert (report.rows_seen, report.rows_converted) == (3, 3)
    assert report.failures == ()
    first = store.get_verdict(legacy_conn, "sid-a")
    assert first is not None
    assert first.verdict == "vocal"
    assert first.coverage_pct == 80.0
    assert first.source == "musixmatch api"
    assert first.language_iso3 == "eng"
    assert (first.n_words, first.n_lines) == (2, 1)
    assert first.pct_witness_red == 0.25
    assert first.override is None and first.override_note is None
    assert first.pipeline_version == PIPELINE_VERSION, (
        "this writer produced the artifact bytes, so it owns the version"
    )
    assert first.computed_at == T_A, (
        "the pipeline judged the track then, not on migration day"
    )
    assert first.words_content_hash is not None
    assert first.updated_at != T_A, "the sync stamp is this machine's, now"
    assert first.deleted_at is None


def test_a_human_override_survives_the_migration(
    legacy_conn: sqlite3.Connection, data_dir: Path
) -> None:
    """It is the one value nothing downstream can recompute."""
    report = _migrate(legacy_conn, data_dir)
    assert report.overrides_carried == 1
    second = store.get_verdict(legacy_conn, "sid-b")
    assert second is not None
    assert second.verdict == "sparse"
    assert second.override == "no-lyrics"
    assert second.override_note == "heard it"
    assert second.effective == "no-lyrics"
    assert second.computed_at == T_B


def test_each_artifact_hashes_to_what_its_row_records(
    legacy_conn: sqlite3.Connection, data_dir: Path
) -> None:
    _migrate(legacy_conn, data_dir)
    for stable_id, expected_words in (("sid-a", 2), ("sid-b", 3)):
        verdict = store.get_verdict(legacy_conn, stable_id)
        assert verdict is not None
        path = karaoke_cache.cache_path(data_dir, stable_id)
        assert path.is_file()
        body = path.read_bytes()
        assert hashlib.sha256(body).hexdigest() == verdict.words_content_hash
        parsed = karaoke_cache.parse_words(body, stable_id)
        assert len(parsed.words) == expected_words
        assert parsed.pipeline_version == PIPELINE_VERSION
        assert parsed.source == verdict.source


def test_word_values_survive_the_round_trip(
    legacy_conn: sqlite3.Connection, data_dir: Path
) -> None:
    _migrate(legacy_conn, data_dir)
    parsed = karaoke_cache.parse_words(
        karaoke_cache.cache_path(data_dir, "sid-a"), "sid-a"
    )
    assert [(w.idx, w.word, w.start_s, w.end_s, w.score, w.witness, w.line_final)
            for w in parsed.words] == [
        (0, "one", 1.0, 1.2, -0.5, "agree", False),
        (1, "two", 1.5, 1.9, -0.4, "contradict", True),
    ]
    assert [w.asr_delta_s for w in parsed.words] == [None, None], (
        "the branch had no asr_delta_s column, so null is the honest value"
    )


def test_a_wordless_verdict_converts_without_an_artifact(
    legacy_conn: sqlite3.Connection, data_dir: Path
) -> None:
    """Instrumental tracks were judged with no alignment; no file must appear."""
    report = _migrate(legacy_conn, data_dir)
    assert report.rows_wordless == 1
    third = store.get_verdict(legacy_conn, "sid-c")
    assert third is not None
    assert third.words_content_hash is None
    assert third.n_words == 0 and third.n_lines is None
    assert not karaoke_cache.cache_path(data_dir, "sid-c").exists()


def test_the_legacy_tables_are_dropped_only_on_a_clean_sweep(
    legacy_conn: sqlite3.Connection, data_dir: Path
) -> None:
    report = _migrate(legacy_conn, data_dir)
    assert report.tables_dropped is True
    remaining = _tables(legacy_conn)
    assert "lyric_verdict_legacy" not in remaining
    assert "lyric_word_legacy" not in remaining
    assert "lyric_verdict" in remaining
    with pytest.raises(legacy_words.LegacyWordsError, match="rename-aside"):
        _migrate(legacy_conn, data_dir)


def test_every_write_is_stamped_and_logged(
    legacy_conn: sqlite3.Connection, data_dir: Path
) -> None:
    """Migrated rows must reach peers; an unlogged row never gets pushed."""
    _migrate(legacy_conn, data_dir)
    logged = [
        str(row[0])
        for row in legacy_conn.execute(
            "SELECT row_pk FROM local_changelog WHERE table_name='lyric_verdict'"
        )
    ]
    assert len(logged) == 4, "three upserts plus the one override carried over"
    assert not legacy_conn.in_transaction


#-----------------------------------------------------------------------------
# dry run
#-----------------------------------------------------------------------------
def test_dry_run_reports_what_would_convert_and_writes_nothing(
    legacy_conn: sqlite3.Connection, data_dir: Path
) -> None:
    report = _migrate(legacy_conn, data_dir, dry_run=True)

    assert (report.rows_seen, report.rows_convertible) == (3, 3)
    assert report.rows_converted == 0
    assert report.tables_dropped is False
    assert report.failures == ()
    assert legacy_conn.execute("SELECT COUNT(*) FROM lyric_verdict").fetchone()[0] == 0
    assert legacy_conn.execute(
        "SELECT COUNT(*) FROM local_changelog WHERE table_name='lyric_verdict'"
    ).fetchone()[0] == 0
    assert not karaoke_cache.cache_dir(data_dir).exists()
    assert {"lyric_verdict_legacy", "lyric_word_legacy"} <= _tables(legacy_conn)
    rendered = legacy_words.format_report(report)
    assert "[DRY-RUN] 3/3 legacy verdicts would convert" in rendered
    assert "legacy tables KEPT" in rendered


def test_a_dry_run_followed_by_a_real_run_converts_everything(
    legacy_conn: sqlite3.Connection, data_dir: Path
) -> None:
    _migrate(legacy_conn, data_dir, dry_run=True)
    report = _migrate(legacy_conn, data_dir)
    assert report.rows_converted == 3
    assert report.tables_dropped is True


#-----------------------------------------------------------------------------
# a row that will not convert
#-----------------------------------------------------------------------------
@pytest.fixture
def broken_conn(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """``sid-b`` has a gap in its word indices: idx 0 then idx 2.

    The retired composite PK guaranteed uniqueness but not contiguity, and the
    artifact writer assigns idx positionally, so accepting the gap would
    silently re-index every word after it.
    """
    use_local_mode(monkeypatch)
    gapped = tuple(
        row for row in _LEGACY_WORDS if not (row[0] == "sid-b" and row[1] == 1)
    )
    db_path = data_dir / "state" / "state.db"
    _seed_legacy_db(db_path, words=gapped)
    connection = state_db.open_rw(db_path)
    try:
        yield connection
    finally:
        connection.close()


def test_a_row_that_cannot_convert_keeps_the_legacy_tables(
    broken_conn: sqlite3.Connection, data_dir: Path
) -> None:
    report = _migrate(broken_conn, data_dir)

    assert report.rows_seen == 3
    assert report.rows_converted == 2
    assert report.tables_dropped is False, (
        "dropping now would destroy the only copy of the row that failed"
    )
    assert {"lyric_verdict_legacy", "lyric_word_legacy"} <= _tables(broken_conn)
    assert len(report.failures) == 1
    assert report.failures[0].startswith("sid-b: ")
    assert "not contiguous" in report.failures[0]
    assert store.get_verdict(broken_conn, "sid-a") is not None
    assert store.get_verdict(broken_conn, "sid-b") is None


def test_the_failure_names_the_row_in_the_printed_report(
    broken_conn: sqlite3.Connection, data_dir: Path
) -> None:
    rendered = legacy_words.format_report(_migrate(broken_conn, data_dir))
    assert "[OK] 2/3 legacy verdicts converted" in rendered
    assert "legacy tables KEPT" in rendered
    assert "[ERROR] 1 row(s) failed:" in rendered
    assert "sid-b" in rendered


def test_the_cli_exits_non_zero_when_a_row_fails(
    broken_conn: sqlite3.Connection,
    data_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A silent exit 0 would let a batch script conclude the job is done."""
    broken_conn.close()
    code = legacy_words.main(
        db_path=data_dir / "state" / "state.db", dry_run=False, s3=None, cfg=None
    )
    assert code == 1
    assert "sid-b" in capsys.readouterr().out


def test_absent_legacy_tables_are_an_error_not_a_no_op(
    conn: sqlite3.Connection, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise a mistyped runbook step reports a successful zero-row sweep."""
    use_local_mode(monkeypatch)
    with pytest.raises(legacy_words.LegacyWordsError, match="not in this database"):
        _migrate(conn, data_dir)
