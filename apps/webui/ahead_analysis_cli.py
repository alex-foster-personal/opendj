"""CLI twin of the ahead-of-time analysis drain HTTP surface (NATIVE-21).

    python -m apps.webui.ahead_analysis_cli coverage
    python -m apps.webui.ahead_analysis_cli retry
    python -m apps.webui.ahead_analysis_cli bump --stable-id S
    python -m apps.webui.ahead_analysis_cli strips --stable-id S [--stable-id T ...]

Talks to the running engine (worktree ports when configured, else the engine
lock file's origin). Prints the JSON response; exits non-zero on any non-2xx.
"""
from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import quote

import httpx

from apps.webui.coverage_drain_cli import _backend_base_url

PREFIX: str = "/api/v1/ahead-analysis"
STRIPS_PATH: str = "/api/v1/library/preview-strips"
REQUEST_TIMEOUT_S: float = 60.0


def request_for(verb: str, stable_id: str | None) -> tuple[str, str]:
    if verb == "coverage":
        return "GET", f"{PREFIX}/coverage"
    if verb == "retry":
        return "POST", f"{PREFIX}/retry"
    if verb == "bump":
        if not stable_id:
            raise ValueError("bump needs --stable-id")
        return "POST", f"{PREFIX}/bump/{quote(stable_id, safe='')}"
    if verb == "strips":
        return "POST", STRIPS_PATH
    raise ValueError(f"unhandled verb {verb!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ahead_analysis_cli", description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=("coverage", "retry", "bump", "strips"))
    parser.add_argument("--stable-id", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        method, path = request_for(args.verb, args.stable_id[0] if args.stable_id else None)
    except ValueError as error:
        parser.error(str(error))
    if args.verb == "strips" and not args.stable_id:
        parser.error("strips needs at least one --stable-id")
    body = {"ids": args.stable_id} if args.verb == "strips" else None
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        response = client.request(method, f"{_backend_base_url()}{path}", json=body)
    sys.stdout.write(json.dumps(response.json(), indent=2) + "\n")
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
