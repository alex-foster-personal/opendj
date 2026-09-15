"""Subprocess worker: open a library-media path and read one byte.

Runs outside the engine process so a kernel-blocked ``open()`` can be killed
without leaking a wedged thread into the parent (#2749 / #766 fleet half).
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: bounded_file_open_worker <path>", file=sys.stderr)
        raise SystemExit(2)
    path = Path(sys.argv[1])
    try:
        with open(path, "rb") as fh:
            fh.read(1)
    except OSError as exc:
        print(f"{exc.errno}:{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    raise SystemExit(0)


if __name__ == "__main__":
    main()
