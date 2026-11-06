"""Queue Open DJ tracks Mixed In Key has not analysed into MIK itself (RBXAN-8322h).

    python -m apps.mik.queue --state-db <state.db> --only-playlists OLTF --only-playlists Viva          # list
    python -m apps.mik.queue --state-db <state.db> --only-playlists OLTF --only-playlists Viva --send   # hand to MIK

MIK has no import API and its store holds macOS bookmark blobs, so we never
write it. Instead the files go through MIK's own import (``open -a``, the
same as dragging them in) and MIK analyses them unattended. Read side: a copy
of the store with its WAL, opened read-only. A track counts as analysed when
MIK holds a key for its path (NFC-compared). Missing files are skipped.

Verified live Thu 5 Nov 2026: 263 tracks sent in batches of 40, MIK's song
count went 1370 -> 1633.

-Claude
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

from apps.mik import mikdb

MIK_APP = "Mixed In Key 11"
STORE = Path.home() / "Library/Application Support/Mixedinkey/Collection11.mikdb"
BATCH = 40
BATCH_PAUSE_S = 3.0


def _nfc(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def _mik_analysed_paths(store: Path) -> set[str]:
    with tempfile.TemporaryDirectory(prefix="mik-queue-") as tmp:
        for side in ("", "-wal", "-shm"):
            src = Path(f"{store}{side}")
            if src.exists():
                shutil.copy2(src, Path(tmp) / src.name)
        songs, _stats = mikdb.read_songs(mikdb.open_ro(Path(tmp) / store.name), with_segments=False)
    return {_nfc(s.path) for s in songs if s.path and s.key_camelot}


def _open_dj_paths(state_db: Path, prefixes: tuple[str, ...]) -> set[str]:
    with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT DISTINCT t.file_path, p.name FROM tracks t "
            "JOIN playlist_memberships m USING (stable_id) JOIN playlists p USING (playlist_id) "
            "WHERE t.deleted_at IS NULL AND t.file_path IS NOT NULL"
        ).fetchall()
    return {_nfc(path) for path, name in rows if not prefixes or name.startswith(prefixes)}


def plan(state_db: Path, prefixes: tuple[str, ...], store: Path = STORE) -> dict[str, object]:
    wanted = _open_dj_paths(state_db, prefixes)
    on_disk = {p for p in wanted if Path(p).is_file()}
    todo = sorted(on_disk - _mik_analysed_paths(store))
    return {"in_scope": len(wanted), "missing_files": len(wanted - on_disk), "to_send": todo}


def send(paths: list[str]) -> list[int]:
    """Hand ``paths`` to MIK's own import; returns each batch's exit code (0 = accepted)."""
    codes = []
    for start in range(0, len(paths), BATCH):
        result = subprocess.run(["open", "-a", MIK_APP, *paths[start : start + BATCH]], check=False)
        codes.append(result.returncode)
        time.sleep(BATCH_PAUSE_S)
    return codes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apps.mik.queue")
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--only-playlists", action="append", default=[], metavar="PREFIX")
    parser.add_argument("--send", action="store_true", help="hand the files to MIK (default: list only)")
    parser.add_argument(
        "--allow-mik-file-tag-writes",
        action="store_true",
        help="required with --send: MIK's default settings rewrite every analysed file's tags (comment, key, "
        "energy), changing size, mtime and hash. Seen live Thu 5 Nov on 263 library files.",
    )
    args = parser.parse_args(argv)
    if args.send and not args.allow_mik_file_tag_writes:
        parser.error("--send rewrites the library files' tags via MIK; pass --allow-mik-file-tag-writes to accept")
    result = plan(args.state_db, tuple(args.only_playlists))
    if args.send:
        result["batch_exit_codes"] = send(result["to_send"])  # type: ignore[arg-type]
    print(json.dumps({**result, "to_send": len(result["to_send"])}, indent=2))  # type: ignore[arg-type]
    return 0 if all(c == 0 for c in result.get("batch_exit_codes", [])) else 1  # type: ignore[union-attr]


if __name__ == "__main__":
    sys.exit(main())
