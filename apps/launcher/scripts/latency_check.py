"""Phase 17 Plan 03 step 3.4 -- FTS5 latency smoke test.

Builds a 5,000-track fixture SQLite DB, invokes
``cargo run --example search_bench -- <query>`` pointed at it, and asserts
P95 < 50ms. Meant to be run locally; CI hooks not wired yet.

Exits 0 on P95 < 50ms, 1 on regression, 2 on toolchain failure.
"""
from __future__ import annotations

import argparse
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_TAURI = REPO_ROOT / "apps" / "launcher" / "src-tauri"

TITLES = ["Neon Orchard", "Velvet Static", "Paper Moon", "Harbor Rain", "Salt Flats",
          "Glass Orchard", "Quiet Rooms", "Open Windows", "Skyline Drive", "Cold Signals"]
ARTISTS = ["Mira Valen", "The Lowlands", "Ana Ferris", "Tomas Rye", "Hollis Wren",
           "Odo Lind", "Pell Arden", "Cass Delaney", "Nina Vale", "June Calloway"]
GENRES = ["Pop", "House", "Techno", "Trance", "Drum and Bass", "Indie", "HipHop"]


def build_fixture(dst: Path, n: int = 5000, seed: int = 42) -> None:
    rng = random.Random(seed)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    conn = sqlite3.connect(dst)
    try:
        conn.executescript(
            """
            CREATE TABLE tracks (
                stable_id TEXT PRIMARY KEY, path TEXT NOT NULL,
                title TEXT, artist TEXT, album TEXT, genre TEXT,
                key TEXT, bpm REAL, duration_ms INTEGER, isrc TEXT, source TEXT
            );
            CREATE VIRTUAL TABLE tracks_fts USING fts5(
                title, artist, album, genre, key, tags,
                tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TABLE tracks_frecency (
                stable_id TEXT PRIMARY KEY,
                plays INTEGER DEFAULT 0, drags INTEGER DEFAULT 0,
                last_played_at INTEGER, last_dragged_at INTEGER
            );
        """
        )
        rows = []
        for i in range(n):
            t = f"{rng.choice(TITLES)} {i}"
            a = rng.choice(ARTISTS)
            g = rng.choice(GENRES)
            rows.append((f"s{i:06d}", f"/music/{i}.mp3", t, a, "Album", g,
                         None, float(rng.randint(80, 180)), None, None, "bench"))
        conn.executemany(
            "INSERT INTO tracks(stable_id, path, title, artist, album, genre, key, bpm, duration_ms, isrc, source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.execute(
            "INSERT INTO tracks_fts(rowid, title, artist, album, genre, key, tags) SELECT rowid, title, artist, album, genre, key, '' FROM tracks"
        )
        conn.commit()
    finally:
        conn.close()


def parse_bench_output(stdout: str) -> tuple[float, float, float]:
    """Parse `p50: X.XXms, p95: Y.YYms, max: Z.ZZms`."""
    m = re.search(r"p50:\s*([\d.]+)ms.*p95:\s*([\d.]+)ms.*max:\s*([\d.]+)ms", stdout)
    if not m:
        raise ValueError(f"unrecognised bench output: {stdout!r}")
    return float(m.group(1)), float(m.group(2)), float(m.group(3))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="mira")  # matches ARTISTS' "Mira Valen"
    ap.add_argument("--tracks", type=int, default=5000)
    ap.add_argument("--p95-budget-ms", type=float, default=50.0)
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as td:
        dbp = Path(td) / "bench.sqlite"
        build_fixture(dbp, n=args.tracks)
        print(f"[latency_check] built {args.tracks}-track fixture at {dbp}")

        env = dict(**os.environ, HYPERK_DB_PATH=str(dbp))
        try:
            out = subprocess.run(
                ["cargo", "run", "--quiet", "--example", "search_bench", "--", args.query],
                cwd=SRC_TAURI, env=env, check=True,
                capture_output=True, text=True, timeout=300,
            )
        except FileNotFoundError:
            print("[latency_check] cargo not on PATH", file=sys.stderr)
            return 2
        except subprocess.CalledProcessError as e:
            print(f"[latency_check] cargo run failed: {e.stderr}", file=sys.stderr)
            return 2

        p50, p95, mx = parse_bench_output(out.stdout)
        print(f"[latency_check] query={args.query!r} tracks={args.tracks} "
              f"p50={p50:.2f}ms p95={p95:.2f}ms max={mx:.2f}ms")
        if p95 > args.p95_budget_ms:
            print(f"[latency_check] REGRESSION: p95 {p95:.2f}ms > budget {args.p95_budget_ms}ms",
                  file=sys.stderr)
            return 1
        print("[latency_check] PASS")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
