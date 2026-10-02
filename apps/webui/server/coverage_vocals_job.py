"""Child process for one auto-drain vocals job: stems in, vocal-cache entry out.

Run by ``apps.webui.server.coverage_drain.vocals_job``, one track at a time,
at lowered scheduling priority. Exits non-zero with the reason on stderr.

    python -m apps.webui.server.coverage_vocals_job --data-dir D \
        --stable-id S --audio-path A --stem-root R [--stem-root R2]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from apps.vocals import from_stems

NICENESS: int = 10


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coverage_vocals_job")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--stable-id", required=True)
    parser.add_argument("--audio-path", type=Path, required=True)
    parser.add_argument("--stem-root", type=Path, action="append", required=True)
    args = parser.parse_args(argv)
    os.nice(NICENESS)
    entry = from_stems.write_from_bundle(
        args.data_dir, args.stable_id, args.audio_path, stem_roots=args.stem_root
    )
    sys.stdout.write(f"[OK] vocals {args.stable_id} coverage_pct={entry.get('coverage_pct')}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
