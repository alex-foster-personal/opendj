"""Capture a live djay leaf-playlist TSAF blob + page-data sidecar.

One-time helper for Phase 3 plan 02 step 1. See
``.planning/phases/03-playlist-sync/03-02-PLAN.md`` for the full runbook.

Manual preamble for the operator:

1. Open djay Pro.
2. Create a playlist named exactly ``music-dj-tools-test-fixture``.
3. Drag ONE local track into it.
4. Quit djay.
5. Run:: ``python scripts/capture_djay_playlist_fixture.py``.
6. Delete the test playlist in djay at your leisure.

Outputs:

* ``tests/fixtures/djay/leaf_playlist.blob`` -- raw TSAF bytes (~100 bytes).
* ``tests/fixtures/djay/leaf_playlist_one_member_page.bin`` -- the 8-byte
  int64-LE page-data blob for the one-member case.
* ``tests/fixtures/djay/leaf_playlist.parse.txt`` -- human-readable dump.

Once the fixture lands, replace
``apps.sync.playlist_tsaf.PLAYLIST_TYPE_LEAF`` with the observed byte
(or pass ``--leaf-type-byte=<byte>`` to the apply CLI).
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

from apps.shared import paths

TEST_NAME = "music-dj-tools-test-fixture"
OUT_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "djay"
CAPTURE_TMP = paths.DATA_DIR / "sync" / "_capture.db"


def _find_leaf_blob(con: sqlite3.Connection) -> tuple[str, bytes] | None:
    for key, data in con.execute(
        "SELECT key, data FROM database2 "
        "WHERE collection = 'mediaItemPlaylists' AND key != 'mediaItemPlaylist-root'"
    ):
        if data is None:
            continue
        if TEST_NAME.encode("utf-8") in data:
            return key, data
    return None


def _find_page_blob(con: sqlite3.Connection, group_key: str) -> bytes | None:
    row = con.execute(
        'SELECT data FROM view_mediaItemPlaylistView_page WHERE "group" = ? LIMIT 1',
        (group_key,),
    ).fetchone()
    return row[0] if row else None


def main() -> int:
    src = paths.DJAY_LIVE_DB
    if not src.exists():
        sys.stderr.write(f"live djay DB missing: {src}\n")
        return 2
    CAPTURE_TMP.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, CAPTURE_TMP)
    con = sqlite3.connect(f"file:{CAPTURE_TMP}?mode=ro", uri=True)
    try:
        hit = _find_leaf_blob(con)
        if hit is None:
            sys.stderr.write(
                f"No leaf playlist named {TEST_NAME!r} found in {src}.\n"
                "Create it inside djay first (see module docstring).\n"
            )
            return 3
        key, data = hit
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        blob_out = OUT_DIR / "leaf_playlist.blob"
        blob_out.write_bytes(data)

        page = _find_page_blob(con, key)
        if page is not None:
            (OUT_DIR / "leaf_playlist_one_member_page.bin").write_bytes(page)

        from apps.sync.playlist_tsaf import parse_playlist_blob

        parsed = parse_playlist_blob(data)
        (OUT_DIR / "leaf_playlist.parse.txt").write_text(
            "\n".join(
                [
                    f"uuid: {parsed.get('uuid', '')}",
                    f"name: {parsed.get('name', '')}",
                    f"type byte: 0x{parsed.get('type', 0):02x}",
                    f"blob size: {len(data)} bytes",
                    f"page blob size: {len(page) if page else 0} bytes",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {blob_out} + parse.txt")
        if page is not None:
            print(f"wrote {OUT_DIR / 'leaf_playlist_one_member_page.bin'}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
