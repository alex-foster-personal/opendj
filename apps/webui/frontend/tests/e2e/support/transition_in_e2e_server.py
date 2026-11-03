"""Seed the AGENT-51 fixture library, then serve the real engine in this process.

The fixture module imports apps.webui at import time, which binds MDT_DATA_DIR.
It therefore runs in a child; this process then calls engine_core.main, which
applies the env contract before its first webui import.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, help="Fixture library directory")
    parser.add_argument("--host", default="127.0.0.1", help="Engine bind host")
    parser.add_argument("--port", required=True, type=int, help="Engine bind port")
    args = parser.parse_args(argv)

    seeded = subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.webui.frontend.tests.e2e.support.transition_in_fixture",
            "--data-dir",
            args.data_dir,
        ],
        check=False,
    )
    if seeded.returncode != 0:
        return seeded.returncode

    from apps.engine_core.__main__ import main as serve

    return serve(
        [
            "serve",
            "--data-dir",
            args.data_dir,
            "--host",
            args.host,
            "--port",
            str(args.port),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
