"""Persisted path availability index (issue #1037, PERF-RB-01).

Resolver-namespaced rows in ``path_availability`` cache rekordbox listing stat
answers across process restarts. Request handlers bulk-load the index, serve
hits immediately, and delegate over-budget refresh to
:mod:`apps.webui.server.path_availability_refresh`.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apps.adapters.rekordbox.config import FILE_EXISTS_TTL_S
from apps.shared.platform_paths import load_path_map
from apps.shared.state import machine_identity

_SQL_CHUNK: int = 500


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
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _is_stale(checked_at: datetime, *, now: datetime | None = None) -> bool:
    anchor = now or datetime.now(UTC)
    return anchor - checked_at > timedelta(seconds=FILE_EXISTS_TTL_S)


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
    out: dict[str, IndexEntry | None] = {path: None for path in wanted}
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
    """Idempotent write of probed paths under ``namespace``."""
    if not rows:
        return
    checked_at = _checked_at_iso()
    conn.executemany(
        "INSERT INTO path_availability("
        "resolver_namespace, logical_path, materialised_size, checked_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(resolver_namespace, logical_path) DO UPDATE SET "
        "materialised_size = excluded.materialised_size, "
        "checked_at = excluded.checked_at",
        [
            (namespace, path, size, checked_at)
            for path, size in rows
        ],
    )


def schedule_background_refresh(paths: Iterable[str]) -> None:
    """Enqueue ``paths`` for async stat + upsert (non-blocking)."""
    from apps.webui.server import path_availability_refresh

    path_availability_refresh.schedule(paths)


__all__ = [
    "IndexEntry",
    "bulk_lookup",
    "resolver_namespace",
    "schedule_background_refresh",
    "upsert_rows",
]
