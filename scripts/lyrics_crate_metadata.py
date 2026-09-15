"""Enrich the crate corpus tracks.json with artist/title metadata from state.db.

A staged crate corpus can contain only
{track_id, source_path, folder, basename} per row, without the artist/title
metadata the lyric fetch stages need. The private corpus is not included
in the public source. This script joins each row onto state.db's `tracks`
table BY FILE PATH and rewrites crate/tracks.json in place as a SUPERSET of
the original rows, adding the OLTF-shaped fields the parametrized
scripts/lyrics_oltf_spike.py subcommands read (artist, title, path, exists,
length_s) plus join provenance (stable_id, db_file_path, meta_join).

Join tiers, DETERMINISTIC only - never fuzzy:
  exact       source_path == tracks.file_path (the post-relink contract)
  air-mirror  tracks.file_path == '/Users/dev/' + source_path minus the
              '~/Music/air-library/' prefix. This is the
              documented air-library mirror rule (~/Music/air-library mirrors
              the Air's /Users/dev home tree); rows the relink has not
              rewritten yet still hold the /Users/dev path.
A row matching zero db rows on both tiers, or more than one distinct
stable_id on the tier that hit, gets excluded_reason set and is EXCLUDED and
COUNTED by every downstream stage - never guessed at (house honest-
denominators rule). Two crate rows resolving to the same stable_id is a
staging bug and fails hard.

Requirements (acceptance, house format)
- ✔︎ [if] a source_path matches no db row under either tier [then] the row is
  written with excluded_reason, and the summary counts it by name.
- ✔︎ [if] a tier matches >1 distinct stable_id [then] excluded_reason says
  ambiguous, never picks one.
- ✔︎ [if] the script runs twice [then] output is identical (derived keys are
  recomputed from the preserved source keys, idempotent).

Usage (repo root):
    uv run --no-sync python scripts/lyrics_crate_metadata.py [--corpus crate]

-Claude
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

AIR_PREFIX: str = str(Path.home() / "Music" / "air-library") + "/"
DEV_PREFIX: str = "/Users/dev/"
# Keys this script derives; stripped before re-deriving so reruns are idempotent.
DERIVED_KEYS: tuple[str, ...] = ("artist", "title", "path", "exists", "length_s",
                                 "stable_id", "db_file_path", "meta_join",
                                 "excluded_reason")


def _artist_str(artists_json: str | None) -> str:
    artists = json.loads(artists_json) if artists_json else []
    return ", ".join(a.strip() for a in artists if a and a.strip())


def _db_rows(conn: sqlite3.Connection, file_path: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT stable_id, title, artists_json, duration_ms, file_path "
        "FROM tracks WHERE file_path = ?", (file_path,)).fetchall()


def _join_one(conn: sqlite3.Connection, source_path: str) -> tuple[str, sqlite3.Row | None, str | None]:
    """(tier, row, error) for one crate row. Exactly one of row/error is set."""
    for tier, candidate in (("exact", source_path),
                            ("air-mirror",
                             DEV_PREFIX + source_path[len(AIR_PREFIX):]
                             if source_path.startswith(AIR_PREFIX) else None)):
        if candidate is None:
            continue
        rows = _db_rows(conn, candidate)
        if len(rows) == 1:
            return tier, rows[0], None
        if len(rows) > 1:
            ids = sorted({r["stable_id"] for r in rows})
            if len(ids) == 1:
                return tier, rows[0], None
            return tier, None, (f"ambiguous: {len(ids)} distinct stable_ids share "
                                f"file_path at tier {tier}")
    return "none", None, "no state.db row matches source_path (exact or air-mirror)"


def main() -> int:
    from apps.shared.paths import STATE_DIR as DEFAULT_STATE_DIR

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", default="crate",
                    help="corpus dir name under lyrics-eval/ (default: crate)")
    ap.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="directory holding state.db (default: apps.shared.paths.STATE_DIR)",
    )
    args = ap.parse_args()
    state_dir = args.state_dir or DEFAULT_STATE_DIR
    eval_dir = state_dir / "lyrics-eval"
    corpus_dir = eval_dir / args.corpus
    state_db = state_dir / "state.db"
    tracks_json = corpus_dir / "tracks.json"
    if not tracks_json.is_file():
        raise SystemExit(f"[ERROR] {tracks_json} missing")
    if not state_db.is_file():
        raise SystemExit(f"[ERROR] {state_db} missing")
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    rows = json.loads(tracks_json.read_text(encoding="utf-8"))
    tiers: dict[str, int] = {}
    excluded: list[str] = []
    seen_stable: dict[str, str] = {}
    for t in rows:
        for k in DERIVED_KEYS:
            t.pop(k, None)
        tier, db_row, err = _join_one(conn, t["source_path"])
        if err is not None:
            t["excluded_reason"] = err
            excluded.append(f"{t['track_id']} ({t['basename']}): {err}")
            tiers["excluded"] = tiers.get("excluded", 0) + 1
            continue
        assert db_row is not None
        if db_row["stable_id"] in seen_stable:
            raise SystemExit(f"[ERROR] {t['track_id']} and {seen_stable[db_row['stable_id']]} "
                             f"both resolve to stable_id {db_row['stable_id']} -- staging bug")
        seen_stable[db_row["stable_id"]] = t["track_id"]
        duration_ms = db_row["duration_ms"]
        t.update({
            "artist": _artist_str(db_row["artists_json"]),
            "title": db_row["title"] or "",
            "path": t["source_path"],
            "exists": Path(t["source_path"]).is_file(),
            "length_s": None if duration_ms is None else round(duration_ms / 1000),
            "stable_id": db_row["stable_id"],
            "db_file_path": db_row["file_path"],
            "meta_join": tier,
        })
        tiers[tier] = tiers.get(tier, 0) + 1
    tracks_json.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"[OK] {tracks_json} rewritten -- join tiers: {tiers} "
          f"(denominator: {len(rows)} {args.corpus} rows)")
    for line in excluded:
        print(f"  [excluded] {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
