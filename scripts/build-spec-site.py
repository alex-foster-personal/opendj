#!/usr/bin/env python3
"""Build the open-dj spec site locally.

One-shot wrapper around `mkdocs build --strict` so contributors can smoke-test
the site without remembering the incantation. CI uses the same command via
`.github/workflows/docs.yml`.

Usage:
    python scripts/build-spec-site.py           # strict build
    python scripts/build-spec-site.py --serve   # live reload on localhost:8000
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--serve",
        action="store_true",
        help="run `mkdocs serve` instead of `mkdocs build --strict`",
    )
    args = parser.parse_args(argv)

    if shutil.which("mkdocs") is None:
        sys.stderr.write(
            "mkdocs not found on PATH. Install with:\n"
            "    pip install -r requirements-docs.txt\n"
        )
        return 2

    cmd = ["mkdocs", "serve"] if args.serve else ["mkdocs", "build", "--strict"]
    print(f"[build-spec-site] running: {' '.join(cmd)}  (cwd={REPO_ROOT})")
    result = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
