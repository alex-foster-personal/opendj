"""Pull cued ``mediaItemUserData`` blobs from the live djay DB.

Safety rails:
  * Aborts if ``djay Pro`` is running (``pgrep -x``).
  * Copies the live DB aside before scanning (read-only copy).
  * Writes one ``<uuid>.<n_cues>.bin`` file per track that carries a
    non-empty ``cuePoints`` array into
    ``tests/fixtures/djay_cue_blobs/``.

Run via::

    python -m scripts.harvest_djay_cues --limit 25

The fixtures are real user data and carry track titles. They are OK to
commit for a personal project, but be aware of the disclosure surface.
See ``tests/fixtures/djay_cue_blobs/README.md`` for provenance.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from apps.shared import paths


def djay_pro_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-x", "djay Pro"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def copy_live_aside() -> Path:
    now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest_dir = paths.DATA_DIR / "djay"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"MediaLibrary.db.wip-04-{now}"
    shutil.copy2(paths.DJAY_LIVE_DB, dest)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.harvest_djay_cues")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("tests/fixtures/djay_cue_blobs"),
    )
    args = parser.parse_args(argv)

    if djay_pro_running():
        print("[harvest] djay Pro is running. Quit it first.", file=sys.stderr)
        return 2

    if not paths.DJAY_LIVE_DB.exists():
        print(f"[harvest] live DB missing: {paths.DJAY_LIVE_DB}", file=sys.stderr)
        return 2

    src = copy_live_aside()
    print(f"[harvest] copied {paths.DJAY_LIVE_DB} -> {src}")

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(f"file:{src}?mode=ro&immutable=1", uri=True)
    try:
        rows = con.execute(
            "SELECT key, data FROM database2 "
            "WHERE collection = 'mediaItemUserData'"
        )
        written = 0
        for key, data in rows:
            if not data or b"\x08cuePoints\x00" not in data:
                continue
            # Heuristic count: count 0x2b ADCMediaItemCuePoint markers.
            n = data.count(b"ADCMediaItemCuePoint")
            if n == 0:
                continue
            uuid = key or ""
            if not uuid:
                continue
            fname = f"{uuid}.{n}.bin"
            (out_dir / fname).write_bytes(data)
            written += 1
            if written >= args.limit:
                break
        print(f"[harvest] wrote {written} fixture blobs to {out_dir}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
