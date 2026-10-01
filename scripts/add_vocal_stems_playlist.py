# /// script
# requires-python = ">=3.10"
# dependencies = ["tinytag>=2.3.2,<3", "httpx"]
# ///
"""Add the 100 clubsauna acapella vocal stems to open-dj as a playlist.

- Parse 'NNN - Artist - Title - vocals.mp3' filenames in the vocals dir.
- INSERT the files into state.db `tracks` (stable_id = sha1(file_path),
  tier 'inferred', duration from tinytag). Idempotent (INSERT OR IGNORE).
- Create playlist via the daemon API, then PUT membership in NNN order.
Fail-fast: any unresolved filename, DB error, or non-2xx API response raises.

MDT_ACAPELLA_VOCALS_DIR names this machine's unpacked acapella-pack vocals
directory (#910: no personal path is hardcoded in tracked code). Required,
and the directory must exist -- an unset or missing value fails fast rather
than globbing zero files and silently doing nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from tinytag import TinyTag, TinyTagException

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "state" / "state.db"
VOCALS_DIR_ENV = "MDT_ACAPELLA_VOCALS_DIR"


def _vocals_dir() -> Path:
    raw = os.environ.get(VOCALS_DIR_ENV)
    if not raw:
        raise RuntimeError(
            f"{VOCALS_DIR_ENV} is not set -- export this machine's unpacked "
            "acapella-pack vocals directory before running this script"
        )
    path = Path(raw)
    if not path.is_dir():
        raise FileNotFoundError(f"{VOCALS_DIR_ENV} does not exist: {path}")
    return path


VOCALS = _vocals_dir()
API = "http://127.0.0.1:8585/api/v1"
PLAYLIST_NAME = "Dan-Vocal-Stems-Max-ish"

FN = re.compile(r"^(\d{2,3})\s*-\s*(.*?)\s*-\s*vocals$")  # NNN - <artist - title>


def parse(fp: Path) -> dict:
    stem = fp.stem  # drop .mp3
    m = FN.match(stem)
    if not m:
        raise ValueError(f"unparseable vocal filename: {fp.name}")
    pos = int(m.group(1))
    body = m.group(2)  # "Artist - Title" (title may itself contain ' - ')
    if " - " in body:
        artist, title = body.split(" - ", 1)
    else:
        artist, title = "", body
    artists = [a.strip() for a in artist.split(",")] if artist else []
    dur_ms = None
    try:
        duration_s = TinyTag.get(str(fp)).duration
        if duration_s:
            dur_ms = round(duration_s * 1000)
    except (TinyTagException, OSError) as e:
        print(f"  warn: no duration for {fp.name}: {e}", file=sys.stderr)
    sid = hashlib.sha1(str(fp).encode("utf-8")).hexdigest()
    return dict(pos=pos, stable_id=sid, title=title.strip(),
                artists_json=json.dumps(artists, ensure_ascii=False),
                file_path=str(fp), duration_ms=dur_ms)


def main() -> None:
    files = sorted(VOCALS.glob("*.mp3"))
    if not files:
        raise SystemExit(f"no vocal mp3s in {VOCALS}")
    rows = [parse(f) for f in files]
    rows.sort(key=lambda r: r["pos"])
    now = datetime.now(UTC).isoformat()
    print(f"parsed {len(rows)} vocal stems (pos {rows[0]['pos']}..{rows[-1]['pos']})")

    # --- 1. insert tracks (idempotent) ---
    con = sqlite3.connect(DB, timeout=15)
    con.execute("PRAGMA busy_timeout=15000;")
    with con:
        for r in rows:
            con.execute(
                """INSERT OR IGNORE INTO tracks
                   (stable_id, stable_id_tier, title, artists_json, album,
                    isrc, duration_ms, file_path, content_hash,
                    created_at, updated_at)
                   VALUES (?, 'inferred', ?, ?, NULL, NULL, ?, ?, NULL, ?, ?)""",
                (r["stable_id"], r["title"], r["artists_json"],
                 r["duration_ms"], r["file_path"], now, now),
            )
    present = con.execute(
        "SELECT COUNT(*) FROM tracks WHERE stable_id IN (%s)"
        % ",".join("?" * len(rows)),
        [r["stable_id"] for r in rows],
    ).fetchone()[0]
    con.close()
    print(f"tracks in DB for these stems: {present}/{len(rows)}")
    if present != len(rows):
        raise SystemExit("track insert incomplete -- aborting before playlist write")

    # --- 2. create (or find) playlist ---
    with httpx.Client(base_url=API, timeout=20) as c:
        existing = c.get("/playlists").raise_for_status().json()
        found = next((p for p in (existing if isinstance(existing, list)
                                  else existing.get("items", existing.get("playlists", [])))
                      if p.get("name") == PLAYLIST_NAME), None)
        if found:
            pid = found["playlist_id"]
            etag = found.get("updated_at") or found.get("etag")
            # fetch fresh etag
            r = c.get(f"/playlists/{pid}").raise_for_status()
            etag = r.headers.get("ETag") or r.json().get("updated_at")
            print(f"reusing existing playlist {pid} (etag {etag})")
        else:
            r = c.post("/playlists", json={"name": PLAYLIST_NAME}).raise_for_status()
            pid = r.json()["playlist_id"]
            etag = r.headers.get("ETag") or r.json()["updated_at"]
            print(f"created playlist {pid} (etag {etag})")

        # --- 3. set membership in order ---
        r = c.put(f"/playlists/{pid}/tracks",
                  headers={"If-Match": etag},
                  json={"stable_ids": [r["stable_id"] for r in rows]})
        if r.status_code >= 300:
            raise SystemExit(f"PUT tracks failed {r.status_code}: {r.text}")
        out = r.json()
        print(f"OK playlist '{out['name']}' id={out['playlist_id']} "
              f"track_count={out['track_count']}")


if __name__ == "__main__":
    main()
