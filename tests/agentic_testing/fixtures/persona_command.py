"""Argv-driven persona subprocess for bounded run harness tests (issue #13261).

Usage: persona_command.py <mode> [arg]

Modes:
  ok           exit 0 and print a stdout marker
  fail         exit 7 and print a stderr marker
  sleep SECS   block until killed or SECS elapse
"""

from __future__ import annotations

import signal
import sys
import time

OK_MARKER = "PERSONA_OK_MARKER"
FAIL_MARKER = "PERSONA_FAIL_MARKER"


def _on_sigterm(_signum: int, _frame: object) -> None:
    raise SystemExit(128 + signal.SIGTERM)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        return 2
    mode = args[0]
    if mode == "ok":
        print(OK_MARKER)
        return 0
    if mode == "fail":
        print(FAIL_MARKER, file=sys.stderr)
        return 7
    if mode == "sleep":
        signal.signal(signal.SIGTERM, _on_sigterm)
        seconds = float(args[1]) if len(args) > 1 else 60.0
        time.sleep(seconds)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
