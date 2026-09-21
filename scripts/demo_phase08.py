"""End-to-end demo of Phase 08 -- smartlists + pairings.

Writes to a tmp state DB (never the live one). Prints a diff table +
captures a transcript suitable for copying into docs/phase08-demo.md.

Usage::

    python -m scripts.demo_phase08           # prints to stdout
    python -m scripts.demo_phase08 --out demo.log

By default the demo uses :class:`FakeWriter` for both RB and djay so
nothing touches a live DB. Pass ``--live`` to swap in the real Phase 3
writers from :func:`apps.smartlists.refresh._build_writers`; unusable
factories soft-fail to None and the materialiser simply skips them.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from apps.shared.pairings import PairingsRepo, ensure_phase08_tables
from apps.smartlists.materializer import Materializer
from apps.smartlists.repo import SmartlistsRepo
from apps.smartlists.triggers import StateEvent, TriggerRunner
from apps.smartlists.writers import FakeWriter

_PHASE5_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tracks (
        stable_id       TEXT PRIMARY KEY,
        stable_id_tier  TEXT NOT NULL,
        title           TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS track_fields (
        stable_id    TEXT NOT NULL,
        field_name   TEXT NOT NULL,
        value_json   TEXT NOT NULL,
        source       TEXT NOT NULL,
        confidence   REAL,
        modified_at  TEXT NOT NULL,
        PRIMARY KEY (stable_id, field_name)
    )
    """,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _seed_library(conn: sqlite3.Connection) -> None:
    rows = [
        ("t00", "Intro Pad",  "Ambient", 90,  3),
        ("t01", "House Warmup", "House",  120, 4),
        ("t02", "Peak House",   "House",  126, 5),
        ("t03", "Big Room",     "House",  128, 5),
        ("t04", "Techno Ride",  "Techno", 130, 4),
        ("t05", "Disco Gold",   "Disco",  118, 4),
        ("t06", "Slow Fade",    "Ambient", 95, 2),
        ("t07", "Drop Track",   "Techno", 135, 5),
    ]
    for sid, title, genre, bpm, rating in rows:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, "
            "created_at, updated_at) VALUES (?, 'isrc', ?, ?, ?)",
            (sid, title, _now(), _now()),
        )
        for field, value in (("genre", genre), ("bpm", bpm), ("rating", rating)):
            conn.execute(
                "INSERT INTO track_fields (stable_id, field_name, "
                "value_json, source, modified_at) VALUES (?, ?, ?, ?, ?)",
                (sid, field, json.dumps(value), "rekordbox", _now()),
            )


def run(out=sys.stdout, *, live: bool = False) -> None:
    out.write("# Phase 08 demo\n\n")

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "state.db"
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            for stmt in _PHASE5_DDL:
                conn.execute(stmt)
            ensure_phase08_tables(conn)
            _seed_library(conn)

            sm_repo = SmartlistsRepo(conn, ensure_schema=False)
            pair_repo = PairingsRepo(conn, ensure_schema=False)

            # --- smartlists ---
            sm_repo.create(
                "House Fresh",
                {
                    "op": "and",
                    "children": [
                        {"field": "genre", "op": "=", "value": "House"},
                        {"field": "bpm", "op": "between", "value": [120, 128]},
                    ],
                },
            )
            sm_repo.create(
                "Top Rated",
                {"field": "rating", "op": ">=", "value": 4},
            )
            sm_repo.create(
                "Techno Peaks",
                {"field": "genre", "op": "=", "value": "Techno"},
            )

            # --- pairings ---
            pair_repo.add("t02", "t03", direction="into",
                          notes="peak transition")
            pair_repo.add("t04", "t07", direction="into",
                          notes="energy climb")
            pair_repo.add("t00", "t01", direction="into")
            pair_repo.add("t05", "t01", direction="either")
            pair_repo.add("t03", "t04", direction="out_of")

            # --- materialise ---
            if live:
                # Real Phase 3 writers; anything unreachable soft-fails to
                # None and drops out of the writer list.
                from apps.smartlists.refresh import _build_writers

                real_writers = _build_writers()
                if real_writers:
                    mat = Materializer(sm_repo, real_writers)
                    rb = dj = None  # rendered below if fakes were used
                else:
                    rb = FakeWriter(vendor="rekordbox")
                    dj = FakeWriter(vendor="djay")
                    mat = Materializer(sm_repo, [rb, dj])
                    out.write(
                        "(live requested but no real writers available; "
                        "falling back to FakeWriter)\n\n"
                    )
            else:
                rb = FakeWriter(vendor="rekordbox")
                dj = FakeWriter(vendor="djay")
                mat = Materializer(sm_repo, [rb, dj])

            out.write("## 1. Dry-run\n\n")
            for r in mat.materialize_all(dry_run=True):
                out.write(
                    f"- {r.smartlist_name}: added={len(r.added_tracks)} "
                    f"total={r.total_tracks}\n"
                )

            out.write("\n## 2. Live run (FakeWriter)\n\n")
            for r in mat.materialize_all(dry_run=False, live=True):
                out.write(
                    f"- {r.smartlist_name}: writers={r.writers_applied} "
                    f"ok={r.ok}\n"
                )

            out.write("\n## 3. RB playlists after run\n\n")
            if rb is not None:
                for name, ids in sorted(rb.playlists.items()):
                    out.write(f"- {name}: {ids}\n")
            else:
                out.write("(live writers used; see vendor DB for details)\n")

            out.write("\n## 4. Simulate tag edit -> trigger\n\n")
            runner = TriggerRunner(
                sm_repo, mat, debounce_seconds=0.0,
                dry_run=False, live=True,
            )
            runner.handle_event(StateEvent(
                kind="track.tag_edited",
                stable_id="t02",
                changed_fields=frozenset({"genre"}),
            ))
            results = runner.run_ready()
            names = sorted(r.smartlist_name for r in results)
            out.write(f"Re-materialised: {names}\n")

            out.write("\n## 5. Pairings graph\n\n")
            for edge in pair_repo.list_all():
                out.write(
                    f"- {edge.from_stable_id} -> {edge.to_stable_id} "
                    f"[{edge.direction}] {edge.notes or ''}\n"
                )
        finally:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m scripts.demo_phase08")
    p.add_argument("--out", type=Path, default=None,
                   help="write transcript to this file instead of stdout")
    p.add_argument(
        "--live", action="store_true",
        help="use real Phase 3 writers (RB + djay) instead of FakeWriter; "
             "requires reachable vendor DBs, otherwise soft-falls back",
    )
    args = p.parse_args(argv)
    if args.out is not None:
        with args.out.open("w") as fh:
            run(out=fh, live=args.live)
        print(f"demo transcript written to {args.out}")
    else:
        run(out=sys.stdout, live=args.live)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
