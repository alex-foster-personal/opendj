"""Retention for superseded own-analysis records (STATE-17).

``analysis`` is keyed by ``(stable_id, backend, backend_version)``, so a new
producer version adds a row beside the old one instead of replacing it. For
the own_* lanes the old row is dead weight: own_waveform.backfill records are
0.5 to 3 MB of JSON each, and on the v1 preview (Mon 5 Oct 2026) 1,090 rows of
1.0.0 sat beside 740 rows of 1.4.0, 853 MB of the 1.5 GB analysis table.

A row is SUPERSEDED, and only then pruned, when all of these hold:

- its backend starts with ``own_`` (third-party and librosa rows are never touched);
- the same ``(stable_id, backend)`` has a row with a strictly HIGHER numeric
  version (versions compare as integer tuples, so 1.10.0 > 1.4.0; a version
  that is not dotted integers is never pruned and never supersedes);
- no ``analysis_canonical`` row points at it (a lane may deliberately keep an
  older version canonical) and no ``analysis_stale`` row names it.

So the highest version of every (track, backend) always survives.

Deletes run in short ``BEGIN IMMEDIATE`` batches so a prune never holds the
writer lock for long and never pins the WAL. ``dry_run`` counts rows and bytes
per backend/version and writes nothing.

CLI: ``python -m apps.analysis.retention <state.db> [--dry-run]``. On a running
engine the prune runs inside the engine (``apps.webui.server.state_maintenance``);
do not point the CLI at a live engine's database.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# ----- config -------------------------------------------------------------------


class CFG:
    BACKEND_PREFIX: str = "own_"
    DELETE_BATCH: int = 50
    BUSY_TIMEOUT_MS: int = 5000


_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")


# ----- policy ---------------------------------------------------------------------


def parse_version(raw: str) -> tuple[int, ...] | None:
    """Dotted-integer version as a tuple, or None when it is anything else."""
    return tuple(int(part) for part in raw.split(".")) if _VERSION_RE.match(raw) else None


def superseded_keys(
    rows: list[tuple[str, str, str]],
    protected: set[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """The ``(stable_id, backend, backend_version)`` keys to prune.

    Pure: ``rows`` are every own-analysis key, ``protected`` the keys a
    canonical pointer or a stale marker names.
    """
    newest: dict[tuple[str, str], tuple[int, ...]] = {}
    for stable_id, backend, version in rows:
        parsed = parse_version(version)
        if parsed is None:
            continue
        key = (stable_id, backend)
        if key not in newest or parsed > newest[key]:
            newest[key] = parsed
    out: list[tuple[str, str, str]] = []
    for stable_id, backend, version in rows:
        parsed = parse_version(version)
        if parsed is None:
            continue
        if parsed < newest[(stable_id, backend)] and (stable_id, backend, version) not in protected:
            out.append((stable_id, backend, version))
    return out


# ----- database -------------------------------------------------------------------


@dataclass
class PruneReport:
    dry_run: bool
    rows: int = 0
    bytes: int = 0
    by_version: dict[str, tuple[int, int]] = field(default_factory=dict)

    def __str__(self) -> str:
        verb = "would prune" if self.dry_run else "pruned"
        detail = ", ".join(f"{k}: {n} rows {b / 1048576:.1f} MB" for k, (n, b) in sorted(self.by_version.items()))
        return f"{verb} {self.rows} rows, {self.bytes / 1048576:.1f} MB" + (f" ({detail})" if detail else "")


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{db_path}?mode=rw", uri=True, isolation_level=None, timeout=CFG.BUSY_TIMEOUT_MS / 1000.0
    )
    conn.execute(f"PRAGMA busy_timeout = {CFG.BUSY_TIMEOUT_MS}")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _protected_keys(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    for table in ("analysis_canonical", "analysis_stale"):
        if _table_exists(conn, table):
            keys.update(
                (str(s), str(b), str(v))
                for s, b, v in conn.execute(f"SELECT stable_id, backend, backend_version FROM {table}")
            )
    return keys


def prune_superseded(db_path: Path, *, dry_run: bool = False) -> PruneReport:
    """Delete (or with ``dry_run`` only count) superseded own-analysis rows."""
    report = PruneReport(dry_run=dry_run)
    conn = _open(Path(db_path))
    try:
        if not _table_exists(conn, "analysis"):
            return report
        rows = [
            (str(s), str(b), str(v))
            for s, b, v in conn.execute(
                "SELECT stable_id, backend, backend_version FROM analysis WHERE substr(backend, 1, ?) = ?",
                (len(CFG.BACKEND_PREFIX), CFG.BACKEND_PREFIX),
            )
        ]
        doomed = superseded_keys(rows, _protected_keys(conn))
        tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for offset in range(0, len(doomed), CFG.DELETE_BATCH):
            batch = doomed[offset : offset + CFG.DELETE_BATCH]
            if not dry_run:
                conn.execute("BEGIN IMMEDIATE")
            try:
                for stable_id, backend, version in batch:
                    size_row = conn.execute(
                        "SELECT length(record_json) FROM analysis "
                        "WHERE stable_id = ? AND backend = ? AND backend_version = ?",
                        (stable_id, backend, version),
                    ).fetchone()
                    if size_row is None:
                        continue
                    if not dry_run:
                        conn.execute(
                            "DELETE FROM analysis WHERE stable_id = ? AND backend = ? AND backend_version = ?",
                            (stable_id, backend, version),
                        )
                    entry = tally[f"{backend} {version}"]
                    entry[0] += 1
                    entry[1] += int(size_row[0] or 0)
                if not dry_run:
                    conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()
    report.by_version = {k: (n, b) for k, (n, b) in tally.items()}
    report.rows = sum(n for n, _ in report.by_version.values())
    report.bytes = sum(b for _, b in report.by_version.values())
    return report


# ----- cli ------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    print(prune_superseded(args.db, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CFG", "PruneReport", "parse_version", "prune_superseded", "superseded_keys"]
