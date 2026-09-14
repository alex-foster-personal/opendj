"""Karaoke lyric verdicts in state.db: the one read/write surface.

Used by BOTH the ingest CLI (``python -m apps.lyrics ingest-state``) and the
daemon router, so the shapes the API returns and the shapes the pipeline
writes can never drift apart.

ONE table, schema _V10: ``lyric_verdict``, one row per track carrying the
no-lyrics verdict, its evidence and provenance, a human ``override`` that
always wins over the computed value, and the sha256 of that track's
``karaoke_words`` artifact. Word timings themselves are NOT here: they live in
the per-track artifact (:mod:`apps.lyrics.karaoke_cache`,
:mod:`apps.lyrics.artifacts`), and ``words_content_hash`` is their ONLY
location record. ``n_words`` / ``n_lines`` are stored so the SQL filters and
the listing enrichment need no join.

The verdict a caller should ACT on is :attr:`LyricVerdict.effective` - never
the raw column, because an override exists precisely to be obeyed.

**Connections.** ``lyric_verdict`` is a SYNCED table, so every write here
stamps through ``sync_stamp.stamped_transaction`` +
``ensure_local_machine`` + ``stamp_and_log``: a row that reaches the table
without passing that choke point syncs as epoch and loses every conflict.
That needs a real file-backed, writable connection with the machine-id file
beside it. A router MUST therefore pass the cloudsync-style write connection
(``apps/webui/server/routes/cloudsync.py:get_cloudsync_write_conn``, which
resolves the DB path from ``request.app.state``), never a connection opened
from a hardcoded ``"data/state/state.db"`` default: that default writes one
machine's verdicts into whichever DB the process happened to start next to.

**Tombstones.** Deletes do not exist; ``apps.lyrics.purge`` sets
``deleted_at`` and re-stamps. A tombstone is sticky:
:func:`upsert_verdict` refuses a tombstoned row unless ``resurrect=True``, so
a purged track that a later batch re-ingests stays purged. Every reader
filters BOTH the row's own tombstone and the parent ``tracks`` tombstone: hard
``DELETE`` of tracks is forbidden repo-wide, so the FK ``ON DELETE CASCADE``
never fires and a tombstoned track would otherwise keep a live-looking
verdict.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from apps.shared.state import sync_stamp
from apps.shared.state.schema import LYRIC_OVERRIDES, LYRIC_VERDICTS

TABLE: str = "lyric_verdict"

#: The 16 columns, in DDL order. Explicit everywhere: ``SELECT *`` would drop
#: a new column on the floor until somebody noticed the dataclass was narrow.
COLUMNS: tuple[str, ...] = (
    "stable_id",
    "verdict",
    "coverage_pct",
    "source",
    "language_iso3",
    "n_words",
    "n_lines",
    "pct_witness_red",
    "override",
    "override_note",
    "pipeline_version",
    "words_content_hash",
    "computed_at",
    "updated_at",
    "origin_device_id",
    "deleted_at",
)

#: ``order='recent'`` sorts on ``computed_at``, NOT ``updated_at``. Under _V10
#: ``updated_at`` is the sync stamp, so a peer's push would silently reorder
#: the triage list by "when a machine last touched the row" rather than "when
#: the pipeline last judged the track", which is what a human triaging asks
#: for.
_ORDER_SQL: dict[str, str] = {
    "suspect": "COALESCE(v.pct_witness_red, -1) DESC, v.coverage_pct ASC",
    "coverage": "v.coverage_pct ASC",
    "recent": "v.computed_at DESC",
}

_SELECT: str = (
    "SELECT " + ", ".join(f"v.{column}" for column in COLUMNS) + " "
    f"FROM {TABLE} v JOIN tracks t ON t.stable_id = v.stable_id"
)
_LIVE: str = "v.deleted_at IS NULL AND t.deleted_at IS NULL"

_SHA256_HEX_LEN: int = 64


class LyricStoreError(RuntimeError):
    """A lyric_verdict read or write was refused. Never swallowed."""


@dataclass(frozen=True)
class LyricVerdict:
    stable_id: str
    verdict: str
    coverage_pct: float | None
    source: str | None
    language_iso3: str | None
    n_words: int | None
    n_lines: int | None
    pct_witness_red: float | None
    override: str | None
    override_note: str | None
    pipeline_version: str
    words_content_hash: str | None
    computed_at: str
    updated_at: str
    origin_device_id: str | None
    deleted_at: str | None

    @property
    def effective(self) -> str:
        """The verdict to act on: a human override always wins."""
        return self.override or self.verdict


#-----------------------------------------------------------------------------
# writes
#-----------------------------------------------------------------------------
def _validated_hash(words_content_hash: str | None, stable_id: str) -> str | None:
    if words_content_hash is None:
        return None
    candidate = words_content_hash.strip()
    if len(candidate) != _SHA256_HEX_LEN or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        raise LyricStoreError(
            f"words_content_hash for {stable_id!r} must be {_SHA256_HEX_LEN} "
            f"lowercase hex chars (a sha256 digest); got {words_content_hash!r}"
        )
    return candidate


def _guard_tombstone(
    conn: sqlite3.Connection, stable_id: str, resurrect: bool
) -> None:
    row = conn.execute(
        f"SELECT deleted_at FROM {TABLE} WHERE stable_id = ?", (stable_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return
    if not resurrect:
        raise LyricStoreError(
            f"lyric_verdict {stable_id!r} was purged (deleted_at={row[0]!r}); "
            "refusing to resurrect it as a side effect of a recompute. Pass "
            "resurrect=True to revive it deliberately."
        )


def upsert_verdict(  # noqa: PLR0913 - one keyword per lyric_verdict column, by design
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    verdict: str,
    coverage_pct: float | None,
    source: str | None,
    language_iso3: str | None,
    n_words: int | None,
    n_lines: int | None,
    pct_witness_red: float | None,
    pipeline_version: str,
    words_content_hash: str | None,
    computed_at: str,
    resurrect: bool,
) -> None:
    """Write the COMPUTED verdict, preserving any human override on the row.

    ``resurrect`` is keyword-only with NO default on purpose: reviving a
    purged track is a deliberate, named act (D13.1), and a default would make
    it the silent behaviour of the next batch run. ``computed_at`` is likewise
    explicit so the legacy migration can carry the original judgement time
    rather than stamping every migrated row with the migration's clock.

    The ``ON CONFLICT`` set list omits ``override`` and ``override_note``:
    that omission IS the override-survives-recompute guarantee.

    ``n_words``/``n_lines``/``words_content_hash`` use ``COALESCE(excluded.*,
    lyric_verdict.*)`` rather than a bare ``excluded.*`` assignment: a caller
    passing ``None`` for these (a coverage-only writer, e.g.
    :mod:`apps.lyrics.library_verdicts`, which never touches word-level data)
    must never NULL out real word data that another writer already put on the
    row. This is the atomicity fix a column-scoped read-then-check cannot be:
    a caller like the backfill checks ``get_verdict(...).words_content_hash``
    before this call to decide whether to skip a track at all, but that check
    and this write are two separate statements, so a real ASR/aligner write
    landing in between them would otherwise get clobbered by this one. Making
    the UPDATE itself preserve non-NULL word columns closes that window
    outright, in the one place every writer funnels through, rather than
    patching each caller's race individually. A caller that legitimately HAS
    fresh word data (:mod:`apps.lyrics.ingest_state`,
    :mod:`apps.lyrics.legacy_words`) always passes real values here, so this
    never blocks a genuine word-data write.
    """
    if verdict not in LYRIC_VERDICTS:
        raise LyricStoreError(f"verdict {verdict!r} not in {list(LYRIC_VERDICTS)}")
    if not pipeline_version:
        raise LyricStoreError(
            f"pipeline_version is required for {stable_id!r}: a row whose hash "
            "cannot be traced to the code that produced it is untraceable."
        )
    digest = _validated_hash(words_content_hash, stable_id)
    machine_id = sync_stamp.ensure_local_machine(conn)
    with sync_stamp.stamped_transaction(conn):
        _guard_tombstone(conn, stable_id, resurrect)
        stamp = sync_stamp.stamp_and_log(conn, TABLE, (stable_id,), machine_id)
        conn.execute(
            f"""
            INSERT INTO {TABLE} (
                stable_id, verdict, coverage_pct, source, language_iso3,
                n_words, n_lines, pct_witness_red, pipeline_version,
                words_content_hash, computed_at, updated_at, origin_device_id,
                deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(stable_id) DO UPDATE SET
                verdict = excluded.verdict,
                coverage_pct = excluded.coverage_pct,
                source = excluded.source,
                language_iso3 = excluded.language_iso3,
                n_words = COALESCE(excluded.n_words, lyric_verdict.n_words),
                n_lines = COALESCE(excluded.n_lines, lyric_verdict.n_lines),
                pct_witness_red = excluded.pct_witness_red,
                pipeline_version = excluded.pipeline_version,
                words_content_hash =
                    COALESCE(excluded.words_content_hash, lyric_verdict.words_content_hash),
                computed_at = excluded.computed_at,
                updated_at = excluded.updated_at,
                origin_device_id = excluded.origin_device_id,
                deleted_at = NULL
            """,
            (
                stable_id, verdict, coverage_pct, source, language_iso3,
                n_words, n_lines, pct_witness_red, pipeline_version, digest,
                computed_at, stamp.updated_at, stamp.origin_device_id,
            ),
        )


def set_override(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    override: str | None,
    note: str | None,
) -> None:
    """Record (or clear, with None) the human verdict for one track."""
    if override is not None and override not in LYRIC_OVERRIDES:
        raise LyricStoreError(f"override {override!r} not in {list(LYRIC_OVERRIDES)}")
    machine_id = sync_stamp.ensure_local_machine(conn)
    with sync_stamp.stamped_transaction(conn):
        stamp = sync_stamp.stamp_and_log(conn, TABLE, (stable_id,), machine_id)
        cursor = conn.execute(
            f"UPDATE {TABLE} SET override = ?, override_note = ?, "
            "updated_at = ?, origin_device_id = ? "
            "WHERE stable_id = ? AND deleted_at IS NULL",
            (override, note, stamp.updated_at, stamp.origin_device_id, stable_id),
        )
        if cursor.rowcount == 0:
            raise LyricStoreError(_missing_row_reason(conn, stable_id))


def _missing_row_reason(conn: sqlite3.Connection, stable_id: str) -> str:
    row = conn.execute(
        f"SELECT deleted_at FROM {TABLE} WHERE stable_id = ?", (stable_id,)
    ).fetchone()
    if row is None:
        return (
            f"no lyric_verdict row for stable_id {stable_id!r}; an override "
            "needs a computed row to override."
        )
    return (
        f"lyric_verdict {stable_id!r} was purged (deleted_at={row[0]!r}); "
        "overriding it would make a purged track visible again."
    )


def tombstone(conn: sqlite3.Connection, stable_id: str) -> None:
    """Soft-delete one verdict row. The purge lever's only write primitive."""
    machine_id = sync_stamp.ensure_local_machine(conn)
    with sync_stamp.stamped_transaction(conn):
        stamp = sync_stamp.stamp_and_log(conn, TABLE, (stable_id,), machine_id)
        cursor = conn.execute(
            f"UPDATE {TABLE} SET deleted_at = ?, updated_at = ?, "
            "origin_device_id = ? WHERE stable_id = ? AND deleted_at IS NULL",
            (stamp.updated_at, stamp.updated_at, stamp.origin_device_id, stable_id),
        )
        if cursor.rowcount == 0:
            raise LyricStoreError(_missing_row_reason(conn, stable_id))


#-----------------------------------------------------------------------------
# reads
#-----------------------------------------------------------------------------
def _verdict_from_row(row: Sequence[Any]) -> LyricVerdict:
    return LyricVerdict(**dict(zip(COLUMNS, row, strict=True)))


def verdict_absence_detail(conn: sqlite3.Connection, stable_id: str) -> str | None:
    """Why a track has no live verdict for the words route, or None if it does.

    Unlike :func:`get_verdict`, this distinguishes "no row" from "tombstoned"
    so the router can return an honest 404 detail without SQL in the handler.
    """
    row = conn.execute(
        f"SELECT deleted_at FROM {TABLE} WHERE stable_id = ?", (stable_id,)
    ).fetchone()
    if row is None:
        return f"no live lyric_verdict row for {stable_id!r}"
    if row[0] is not None:
        return f"lyric_verdict {stable_id!r} is tombstoned"
    track = conn.execute(
        "SELECT deleted_at FROM tracks WHERE stable_id = ?", (stable_id,)
    ).fetchone()
    if track is None or track[0] is not None:
        return f"no live lyric_verdict row for {stable_id!r}"
    return None


def get_verdict(conn: sqlite3.Connection, stable_id: str) -> LyricVerdict | None:
    """The live verdict, or None. A tombstoned row reads as absent."""
    row = conn.execute(
        f"{_SELECT} WHERE {_LIVE} AND v.stable_id = ?", (stable_id,)
    ).fetchone()
    return _verdict_from_row(row) if row is not None else None


def bulk_verdicts(
    conn: sqlite3.Connection, stable_ids: Sequence[str]
) -> dict[str, LyricVerdict]:
    """One SELECT for a listing page's worth of verdicts, keyed by stable_id.

    Missing ids are simply absent from the dict: absence IS the honest "no
    lyric data yet" state, never a placeholder.
    """
    if not stable_ids:
        return {}
    marks = ",".join("?" for _ in stable_ids)
    rows = conn.execute(
        f"{_SELECT} WHERE {_LIVE} AND v.stable_id IN ({marks})", tuple(stable_ids)
    )
    return {row[0]: _verdict_from_row(row) for row in rows}


def list_verdicts(
    conn: sqlite3.Connection,
    *,
    limit: int,
    offset: int,
    verdict: str | None,
    order: str,
) -> list[LyricVerdict]:
    """The triage listing. ``order='suspect'`` puts the least trustworthy
    first, which is the only ordering that makes a 1,000-track library
    actionable. The verdict filter is on the EFFECTIVE value."""
    if order not in _ORDER_SQL:
        raise LyricStoreError(f"unknown order {order!r}, not in {sorted(_ORDER_SQL)}")
    where, params = _LIVE, []
    if verdict is not None:
        if verdict not in LYRIC_VERDICTS:
            raise LyricStoreError(f"verdict {verdict!r} not in {list(LYRIC_VERDICTS)}")
        where = f"{_LIVE} AND COALESCE(v.override, v.verdict) = ?"
        params.append(verdict)
    rows = conn.execute(
        f"{_SELECT} WHERE {where} ORDER BY {_ORDER_SQL[order]} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return [_verdict_from_row(row) for row in rows]


def count_verdicts(conn: sqlite3.Connection) -> dict[str, int]:
    """Effective-verdict histogram over LIVE rows: what the library looks like."""
    return {
        str(row[0]): int(row[1])
        for row in conn.execute(
            f"SELECT COALESCE(v.override, v.verdict) AS effective, COUNT(*) "
            f"FROM {TABLE} v JOIN tracks t ON t.stable_id = v.stable_id "
            f"WHERE {_LIVE} GROUP BY effective"
        )
    }


def verdicts_by_source(
    conn: sqlite3.Connection, source_prefix: str
) -> list[LyricVerdict]:
    """Every LIVE row whose source starts with ``source_prefix``.

    The read half of the licensing purge (:mod:`apps.lyrics.purge`): one
    provider's rows, so the caller can tombstone them and remove their words
    from every store.
    """
    rows = conn.execute(
        f"{_SELECT} WHERE {_LIVE} AND v.source LIKE ? ORDER BY v.stable_id",
        (f"{source_prefix}%",),
    )
    return [_verdict_from_row(row) for row in rows]


__all__ = [
    "COLUMNS",
    "TABLE",
    "LyricStoreError",
    "LyricVerdict",
    "bulk_verdicts",
    "count_verdicts",
    "get_verdict",
    "list_verdicts",
    "set_override",
    "tombstone",
    "upsert_verdict",
    "verdict_absence_detail",
    "verdicts_by_source",
]
