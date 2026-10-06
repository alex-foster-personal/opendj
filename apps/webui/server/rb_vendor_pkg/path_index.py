"""Persisted path availability index (issue #1037, PERF-RB-01).

Resolver-namespaced rows in ``path_availability`` cache rekordbox listing stat
answers across process restarts. Request handlers bulk-load the index, serve
hits immediately, and hand over-budget or stale paths to
:mod:`apps.webui.server.path_availability_refresh`.

Lives in the web UI layer, not ``apps.shared.state``: it reads the rekordbox
adapter's path resolver and is fed by a web UI thread, and ``apps.shared``
imports nothing else from ``apps`` (the ``shared-is-the-stable-core``
import-linter contract). The table DDL itself stays in the shared ladder
(``apps/shared/state/migrations_v18.py``) because the schema is shared.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apps.adapters.rekordbox.config import FILE_EXISTS_TTL_S
from apps.adapters.rekordbox.paths import resolve_asset_path
from apps.shared import fs_residency
from apps.shared.platform_paths import load_path_map
from apps.shared.state import machine_identity

_SQL_CHUNK: int = 500
#: Age past which an UNCHANGED probe answer is written back (PERF-RB-05). Ten
#: minutes: twenty TTLs, so the index still tracks disk truth across restarts.
UNCHANGED_REWRITE_AFTER_S: float = 600.0


@dataclass(frozen=True)
class IndexEntry:
    """One ``path_availability`` row plus derived staleness."""

    materialised_size: int | None
    checked_at: datetime
    stale: bool


def _parse_checked_at(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    return datetime.fromisoformat(text).astimezone(UTC)


def _checked_at_iso(when: datetime | None = None) -> str:
    stamp = (when or datetime.now(UTC)).astimezone(UTC)
    return stamp.isoformat(timespec="microseconds")


def _is_stale(checked_at: datetime, *, now: datetime | None = None) -> bool:
    anchor = now or datetime.now(UTC)
    return anchor - checked_at > timedelta(seconds=FILE_EXISTS_TTL_S)


def stat_logical_path(path: str) -> int | None:
    """Materialised size of a library path after containment mapping.

    None when the path does not resolve, is missing, is not a file, or is a
    dataless stub. This is the background refresher's stat; request handlers
    stat through ``track_rows._stat_size`` so the per-request budget stays
    observable in one place.
    """
    resolved = resolve_asset_path(path).resolved
    if resolved is None:
        return None
    return fs_residency.materialised_size(resolved)


def resolver_namespace(data_dir: Path) -> str:
    """Stable fingerprint of the active path map plus machine identity."""
    path_map = load_path_map(data_dir)
    entries_json = json.dumps(
        [{"from": src, "to": dst} for src, dst in path_map.entries],
        sort_keys=True,
        separators=(",", ":"),
    )
    machine_id = machine_identity.get_or_create_machine_id(Path(data_dir))
    payload = f"{entries_json}:{machine_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def bulk_lookup(
    conn: sqlite3.Connection,
    namespace: str,
    paths: Sequence[str],
) -> dict[str, IndexEntry | None]:
    """Load index rows for ``paths`` under ``namespace``."""
    wanted = list(dict.fromkeys(p for p in paths if p))
    if not wanted:
        return {}
    now = datetime.now(UTC)
    out: dict[str, IndexEntry | None] = dict.fromkeys(wanted)
    for offset in range(0, len(wanted), _SQL_CHUNK):
        chunk = wanted[offset : offset + _SQL_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT logical_path, materialised_size, checked_at "
            "FROM path_availability "
            f"WHERE resolver_namespace = ? AND logical_path IN ({placeholders})",
            (namespace, *chunk),
        ).fetchall()
        for logical_path, materialised_size, checked_at in rows:
            parsed = _parse_checked_at(str(checked_at))
            out[str(logical_path)] = IndexEntry(
                materialised_size=(
                    None if materialised_size is None else int(materialised_size)
                ),
                checked_at=parsed,
                stale=_is_stale(parsed, now=now),
            )
    return out


def upsert_rows(
    conn: sqlite3.Connection,
    namespace: str,
    rows: Sequence[tuple[str, int | None]],
) -> None:
    """Idempotent write of probed paths under ``namespace``.

    A row whose answer did not change is rewritten only once its stored
    ``checked_at`` is older than :data:`UNCHANGED_REWRITE_AFTER_S` (PERF-RB-05).
    Inside the process the L1 cache (``config._FILE_EXISTS_CACHE``) already
    holds the fresh answer, so the only thing an unchanged rewrite bought was a
    newer ``checked_at`` for the next process, and ``checked_at`` is indexed.
    With ``FILE_EXISTS_TTL_S`` at 30 s, shorter than one All Tracks walk, every
    walk rewrote every row: on the silver preview (Mon 5 Oct 2026) that was the
    whole of a 300-500 MB WAL, about 1.5 MB/s, all ``path_availability`` and
    ``idx_path_availability_checked`` pages. A changed size is always written.
    """
    if not rows:
        return
    now = datetime.now(UTC)
    checked_at = _checked_at_iso(now)
    rewrite_before = _checked_at_iso(now - timedelta(seconds=UNCHANGED_REWRITE_AFTER_S))
    # ONE transaction for the whole batch (STATE-20). Handles from
    # ``state_db.open_rw`` are autocommit, so a bare executemany committed
    # every row on its own: on installed build 9 (Tue 6 Oct 2026) that was
    # 2,070 of 2,347 WAL commits, each rewriting the same leaf pages again.
    # A caller already inside a transaction keeps ownership of it.
    owns_txn = not conn.in_transaction
    if owns_txn:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany(
            "INSERT INTO path_availability("
            "resolver_namespace, logical_path, materialised_size, checked_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(resolver_namespace, logical_path) DO UPDATE SET "
            "materialised_size = excluded.materialised_size, "
            "checked_at = excluded.checked_at "
            "WHERE path_availability.materialised_size IS NOT excluded.materialised_size "
            "OR path_availability.checked_at < ?",
            [(namespace, path, size, checked_at, rewrite_before) for path, size in rows],
        )
        if owns_txn:
            conn.execute("COMMIT")
    except BaseException:
        if owns_txn and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


__all__ = [
    "IndexEntry",
    "bulk_lookup",
    "resolver_namespace",
    "stat_logical_path",
    "upsert_rows",
]
