"""Re-derive ``analysis.key_openkey`` from ``key_camelot`` via canon.

The #1478 bug was a whole-table one-position rotation of `_OPENKEY_MAJOR` /
`_OPENKEY_MINOR`. Every analysis row written before that fix has a
`key_openkey` that disagrees with `key_camelot` under the corrected rule.
Detecting "the old value" by pattern (e.g. 8A+8m) would miss 23 of 24 keys, so
this script never pattern-matches: it re-derives from `key_camelot`.

    python -m scripts.migrate_key_openkey --db <state.db>           # dry-run
    python -m scripts.migrate_key_openkey --db <state.db> --apply   # rewrite
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from apps.analysis_key import canon
from apps.shared.paths import STATE_DB


def _expected_openkey(camelot: str | None) -> str | None:
    if not camelot:
        return None
    try:
        return canon.to_open_key(canon.from_camelot(str(camelot)))
    except ValueError:
        return None


def _rewrite_segments(container: dict[str, Any]) -> None:
    """Re-derive key_openkey on any key-segment list we can find."""
    segs_block = container.get("segments")
    segs: list[Any] | None = None
    if isinstance(segs_block, dict):
        maybe = segs_block.get("segments")
        if isinstance(maybe, list):
            segs = maybe
    elif isinstance(segs_block, list):
        segs = segs_block
    if not segs:
        return
    for seg in segs:
        if not isinstance(seg, dict):
            continue
        expected = _expected_openkey(seg.get("key_camelot"))
        if expected is None:
            continue
        seg["key_openkey"] = expected


def rewrite_record_json(raw: str, expected_openkey: str) -> str | None:
    """Set top-level key_openkey and any lanes.key segment openkeys.

    Returns None when the blob is not JSON, so the column can still be
    updated without inventing a blob.
    """
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data["key_openkey"] = expected_openkey
    lanes = data.get("lanes")
    if isinstance(lanes, dict):
        key_lane = lanes.get("key")
        if isinstance(key_lane, dict):
            payload = key_lane.get("payload")
            target = payload if isinstance(payload, dict) else key_lane
            camelot = None
            if isinstance(target, dict):
                camelot = target.get("camelot") or target.get("key_camelot")
            payload_expected = _expected_openkey(camelot) if camelot else expected_openkey
            if isinstance(target, dict) and "openkey" in target and payload_expected:
                target["openkey"] = payload_expected
            if isinstance(target, dict):
                _rewrite_segments(target)
            _rewrite_segments(key_lane)
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _require_analysis_table(conn: sqlite3.Connection, db: Path) -> None:
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "analysis" not in names:
        raise SystemExit(
            f"[migrate-key-openkey] no analysis table in {db}; this is not a "
            "production analysis store"
        )


def _scan(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    rows = conn.execute(
        "SELECT stable_id, backend, backend_version, key_camelot, key_openkey, record_json "
        "FROM analysis"
    ).fetchall()
    by_backend: dict[str, dict[str, int]] = defaultdict(
        lambda: {"n_total": 0, "n_ok": 0, "n_affected": 0, "n_unparseable": 0}
    )
    affected: list[dict[str, Any]] = []
    for row in rows:
        backend = str(row["backend"])
        stats = by_backend[backend]
        stats["n_total"] += 1
        expected = _expected_openkey(row["key_camelot"])
        if expected is None:
            stats["n_unparseable"] += 1
            continue
        if row["key_openkey"] == expected:
            stats["n_ok"] += 1
            continue
        stats["n_affected"] += 1
        affected.append(
            {
                "stable_id": row["stable_id"],
                "backend": backend,
                "backend_version": row["backend_version"],
                "key_camelot": row["key_camelot"],
                "key_openkey": row["key_openkey"],
                "expected": expected,
                "record_json": row["record_json"],
            }
        )
    return affected, by_backend


def _print_table(by_backend: dict[str, dict[str, int]]) -> None:
    totals = {"n_total": 0, "n_ok": 0, "n_affected": 0, "n_unparseable": 0}
    print(f"{'backend':<28} {'n_total':>8} {'n_ok':>8} {'n_affected':>11} {'n_unparseable':>14}")
    for backend in sorted(by_backend):
        stats = by_backend[backend]
        print(
            f"{backend:<28} {stats['n_total']:>8} {stats['n_ok']:>8} "
            f"{stats['n_affected']:>11} {stats['n_unparseable']:>14}"
        )
        for key in totals:
            totals[key] += stats[key]
    print(
        f"{'TOTAL':<28} {totals['n_total']:>8} {totals['n_ok']:>8} "
        f"{totals['n_affected']:>11} {totals['n_unparseable']:>14}"
    )


def dry_run(db: Path) -> dict[str, dict[str, int]]:
    if not db.exists():
        raise SystemExit(f"[migrate-key-openkey] db not found: {db}")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=1")
        _require_analysis_table(conn, db)
        _affected, by_backend = _scan(conn)
    finally:
        conn.close()
    _print_table(by_backend)
    return by_backend


def apply(db: Path) -> dict[str, dict[str, int]]:
    if not db.exists():
        raise SystemExit(f"[migrate-key-openkey] db not found: {db}")
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        _require_analysis_table(conn, db)
        affected, by_backend = _scan(conn)
        conn.execute("BEGIN")
        for row in affected:
            new_json = rewrite_record_json(row["record_json"], row["expected"])
            if new_json is None:
                conn.execute(
                    "UPDATE analysis SET key_openkey = ? "
                    "WHERE stable_id=? AND backend=? AND backend_version=?",
                    (row["expected"], row["stable_id"], row["backend"], row["backend_version"]),
                )
            else:
                conn.execute(
                    "UPDATE analysis SET key_openkey = ?, record_json = ? "
                    "WHERE stable_id=? AND backend=? AND backend_version=?",
                    (
                        row["expected"],
                        new_json,
                        row["stable_id"],
                        row["backend"],
                        row["backend_version"],
                    ),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _print_table(by_backend)
    n_applied = sum(s["n_affected"] for s in by_backend.values())
    print(f"[migrate-key-openkey] applied {n_applied} row(s)")
    return by_backend


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=STATE_DB, help=f"default {STATE_DB}")
    parser.add_argument("--apply", action="store_true", help="rewrite; default is dry-run")
    args = parser.parse_args(argv)
    if args.apply:
        apply(args.db)
    else:
        dry_run(args.db)
        print("[migrate-key-openkey] dry-run; pass --apply to rewrite")
    return 0


if __name__ == "__main__":
    sys.exit(main())
