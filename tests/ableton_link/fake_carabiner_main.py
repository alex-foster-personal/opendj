"""A stand-in Carabiner EXECUTABLE for the supervisor tests: run as a real child process.

``python -m tests.ableton_link.fake_carabiner_main --mode serve --port N --daemon`` takes the same
``--port``/``--daemon`` flags the engine passes the real binary and serves Carabiner's ``status``
line protocol (``SimCarabiner``) until it is signalled. ``--mode exit`` exits 3 at once (a binary
that cannot start); ``--mode silent`` stays up and never listens (a binary that never answers).
The real Carabiner is not runnable on Linux; this is the process the supervisor really spawns,
kills, respawns and terminates.
"""

from __future__ import annotations

import argparse
import shlex
import sys
import time
from pathlib import Path

from tests.ableton_link.carabiner_sim import SimCarabiner

REPO_ROOT = Path(__file__).resolve().parents[2]


def write_fake_binary(directory: Path, mode: str) -> Path:
    """An executable ``Carabiner`` in ``directory`` that execs this module (same pid) in ``mode``."""
    path = directory / "Carabiner"
    path.write_text(
        "#!/bin/sh\n"
        f"cd {shlex.quote(str(REPO_ROOT))}\n"
        f"exec {shlex.quote(sys.executable)} -m tests.ableton_link.fake_carabiner_main --mode {mode} \"$@\"\n"
    )
    path.chmod(0o755)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["serve", "exit", "silent"], required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--daemon", action="store_true")
    args = parser.parse_args(argv)
    if not args.daemon:
        print("[ERROR] the engine must pass --daemon, as the launchd plist does", file=sys.stderr)
        return 2
    if args.mode == "exit":
        print("[ERROR] fake Carabiner refusing to start", file=sys.stderr)
        return 3
    if args.mode == "serve":
        SimCarabiner(peers=0, port=args.port)
    elif args.mode == "silent":
        pass
    else:
        raise AssertionError(args.mode)
    print(f"[OK] fake Carabiner {args.mode} on :{args.port}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main())
