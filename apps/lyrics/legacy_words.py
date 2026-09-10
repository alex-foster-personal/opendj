"""One-shot conversion of the branch-era lyric tables into _V10 rows + artifacts.

`python -m apps.lyrics migrate-legacy-words [--dry-run]`.

The karaoke feature branch stored words in a ``lyric_word`` table beside an
11-column ``lyric_verdict``. The D13.6 runbook renames both aside to
``lyric_verdict_legacy`` / ``lyric_word_legacy`` (dropping
``idx_lyric_verdict_red`` and ``idx_lyric_word_time``, which follow their
tables through the rename and would otherwise make _V10's bare
``CREATE INDEX`` fail), sets ``schema_meta`` to 7 and applies main's ladder to
9. This command then converts the renamed-aside data: one ``karaoke_words``
artifact per track (pushed in cloud mode exactly like the producer) plus one
16-column row carrying ``pipeline_version`` = the CURRENT
:data:`apps.lyrics.karaoke_cache.PIPELINE_VERSION` (this writer produced the
bytes, so it owns the version) and the ORIGINAL ``computed_at`` (the pipeline
judged the track then, not now).

Rows arrive with ``resurrect=False``: legacy data cannot revive a track that a
purge has already tombstoned on this machine.

The legacy tables are DROPPED only when every row converted. A partial
migration keeps them, so the re-run has something to finish, and the counts
say plainly how many rows are still there.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.cloud import asset_store
from apps.cloud.config import CloudConfig
from apps.lyrics import store
from apps.lyrics.artifacts import produce_words_artifact
from apps.lyrics.karaoke_cache import PIPELINE_VERSION
from apps.shared.state import db as state_db
from apps.shared.state import sync_stamp

LEGACY_VERDICT_TABLE: str = "lyric_verdict_legacy"
LEGACY_WORD_TABLE: str = "lyric_word_legacy"

_LEGACY_VERDICT_COLUMNS: tuple[str, ...] = (
    "stable_id",
    "verdict",
    "coverage_pct",
    "source",
    "language_iso3",
    "n_words",
    "pct_witness_red",
    "override",
    "override_note",
    "computed_at",
)
_LEGACY_WORD_COLUMNS: tuple[str, ...] = (
    "idx",
    "word",
    "start_s",
    "end_s",
    "score",
    "witness",
    "line_final",
)


class LegacyWordsError(RuntimeError):
    """The legacy tables are absent or malformed. Never guessed around."""


@dataclass(frozen=True)
class LegacyMigrationReport:
    """Honest counts: rows seen, rows converted, what is still on disk."""

    dry_run: bool
    rows_seen: int
    #: Rows whose legacy words read back cleanly, so a real run would write
    #: them. This is what ``--dry-run`` reports; it writes nothing.
    rows_convertible: int
    rows_converted: int
    rows_wordless: int
    overrides_carried: int
    tables_dropped: bool
    failures: tuple[str, ...]


def _require_legacy_tables(conn: sqlite3.Connection) -> None:
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?)",
            (LEGACY_VERDICT_TABLE, LEGACY_WORD_TABLE),
        )
    }
    missing = sorted({LEGACY_VERDICT_TABLE, LEGACY_WORD_TABLE} - present)
    if missing:
        raise LegacyWordsError(
            f"legacy tables {missing} are not in this database; run the D13.6 "
            "rename-aside step first, or there is nothing to migrate."
        )


def _legacy_words(conn: sqlite3.Connection, stable_id: str) -> list[dict[str, Any]]:
    """The track's words as producer input, with contiguity verified.

    The retired composite primary key guaranteed uniqueness but not
    contiguity, and the artifact writer assigns ``idx`` positionally, so a gap
    would silently re-index every word after it.
    """
    rows = conn.execute(
        f"SELECT {', '.join(_LEGACY_WORD_COLUMNS)} FROM {LEGACY_WORD_TABLE} "
        "WHERE stable_id = ? ORDER BY idx",
        (stable_id,),
    ).fetchall()
    words: list[dict[str, Any]] = []
    for position, row in enumerate(rows):
        entry = dict(zip(_LEGACY_WORD_COLUMNS, row, strict=True))
        if int(entry["idx"]) != position:
            raise LegacyWordsError(
                f"{stable_id}: legacy word indices are not contiguous from 0 "
                f"(position {position} carries idx {entry['idx']!r})"
            )
        entry.pop("idx")
        entry["line_final"] = bool(entry["line_final"])
        words.append(entry)
    return words


def _convert_one(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    legacy: dict[str, Any],
    words: list[dict[str, Any]],
    s3: asset_store.AssetS3Client | None,
    cfg: CloudConfig | None,
) -> None:
    """Write one legacy row as an artifact + _V10 row."""
    stable_id = str(legacy["stable_id"])
    content_hash: str | None = None
    n_words: int | None = legacy["n_words"]
    n_lines: int | None = None
    if words:
        artifact = produce_words_artifact(
            conn,
            data_dir=data_dir,
            stable_id=stable_id,
            source=str(legacy["source"] or "legacy"),
            words=words,
            s3=s3,
            cfg=cfg,
        )
        content_hash, n_words, n_lines = (
            artifact.content_hash, artifact.n_words, artifact.n_lines
        )
    store.upsert_verdict(
        conn,
        stable_id=stable_id,
        verdict=str(legacy["verdict"]),
        coverage_pct=legacy["coverage_pct"],
        source=legacy["source"],
        language_iso3=legacy["language_iso3"],
        n_words=n_words,
        n_lines=n_lines,
        pct_witness_red=legacy["pct_witness_red"],
        pipeline_version=PIPELINE_VERSION,
        words_content_hash=content_hash,
        computed_at=str(legacy["computed_at"]),
        resurrect=False,
    )


def _carry_override(conn: sqlite3.Connection, legacy: dict[str, Any]) -> bool:
    """Re-apply a human override. It is the one value nothing can recompute."""
    if legacy["override"] is None:
        return False
    store.set_override(
        conn,
        stable_id=str(legacy["stable_id"]),
        override=str(legacy["override"]),
        note=legacy["override_note"],
    )
    return True


def _drop_legacy_tables(conn: sqlite3.Connection) -> None:
    conn.execute(f"DROP TABLE {LEGACY_WORD_TABLE}")
    conn.execute(f"DROP TABLE {LEGACY_VERDICT_TABLE}")


def migrate_legacy_words(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    s3: asset_store.AssetS3Client | None,
    cfg: CloudConfig | None,
    dry_run: bool,
) -> LegacyMigrationReport:
    """Convert every legacy row. Drops the legacy tables only on a clean sweep."""
    _require_legacy_tables(conn)
    rows = conn.execute(
        f"SELECT {', '.join(_LEGACY_VERDICT_COLUMNS)} FROM {LEGACY_VERDICT_TABLE} "
        "ORDER BY stable_id"
    ).fetchall()
    legacy_rows = [dict(zip(_LEGACY_VERDICT_COLUMNS, row, strict=True)) for row in rows]
    convertible = 0
    converted = 0
    wordless = 0
    overrides = 0
    failures: list[str] = []
    for legacy in legacy_rows:
        try:
            words = _legacy_words(conn, str(legacy["stable_id"]))
            if not dry_run:
                _convert_one(
                    conn, data_dir=data_dir, legacy=legacy, words=words, s3=s3, cfg=cfg
                )
                overrides += int(_carry_override(conn, legacy))
                converted += 1
        except (LegacyWordsError, ValueError, RuntimeError, sqlite3.Error) as error:
            failures.append(f"{legacy['stable_id']}: {error}")
        else:
            convertible += 1
            wordless += int(not words)
    complete = not dry_run and converted == len(legacy_rows)
    if complete:
        _drop_legacy_tables(conn)
    return LegacyMigrationReport(
        dry_run=dry_run,
        rows_seen=len(legacy_rows),
        rows_convertible=convertible,
        rows_converted=converted,
        rows_wordless=wordless,
        overrides_carried=overrides,
        tables_dropped=complete,
        failures=tuple(failures),
    )


def format_report(report: LegacyMigrationReport) -> str:
    """The operator-facing summary the CLI prints."""
    if report.dry_run:
        body = [
            f"[DRY-RUN] {report.rows_convertible}/{report.rows_seen} legacy "
            f"verdicts would convert ({report.rows_wordless} have no words); "
            "legacy tables KEPT"
        ]
    else:
        body = [
            f"[OK] {report.rows_converted}/{report.rows_seen} legacy verdicts "
            f"converted ({report.rows_wordless} had no words, "
            f"{report.overrides_carried} human overrides carried over); legacy "
            f"tables {'dropped' if report.tables_dropped else 'KEPT'}"
        ]
    if report.failures:
        body.append(f"[ERROR] {len(report.failures)} row(s) failed:")
        body.extend(f"        {failure}" for failure in report.failures[:10])
    if report.dry_run:
        body.append("[..] nothing written; re-run without --dry-run")
    return "\n".join(body)


def main(
    *,
    db_path: Path | None,
    dry_run: bool,
    s3: asset_store.AssetS3Client | None,
    cfg: CloudConfig | None,
) -> int:
    """CLI entry point: migrate, print the counts, exit non-zero on any failure."""
    conn = state_db.open_rw(db_path)
    try:
        report = migrate_legacy_words(
            conn,
            data_dir=sync_stamp.data_dir_for_connection(conn),
            s3=s3,
            cfg=cfg,
            dry_run=dry_run,
        )
    finally:
        conn.close()
    print(format_report(report))
    return 1 if report.failures else 0


__all__ = [
    "LEGACY_VERDICT_TABLE",
    "LEGACY_WORD_TABLE",
    "LegacyMigrationReport",
    "LegacyWordsError",
    "format_report",
    "main",
    "migrate_legacy_words",
]
