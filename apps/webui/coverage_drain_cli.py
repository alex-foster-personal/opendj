"""CLI twin of the coverage auto-drain HTTP surface.

    python -m apps.webui.coverage_drain_cli status
    python -m apps.webui.coverage_drain_cli start | stop
    python -m apps.webui.coverage_drain_cli enable | disable
    python -m apps.webui.coverage_drain_cli retry

Talks to the running engine (worktree ports when configured, else the engine
lock file's origin). Prints the JSON response; exits non-zero on any non-2xx.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

from apps.opendj_cli.api_cli import resolve_backend_base_url

PREFIX: str = "/api/v1/coverage-drain"
REQUEST_TIMEOUT_S: float = 30.0
Request = tuple[str, str, dict[str, Any] | None]
VERBS: dict[str, Request] = {
    "status": ("GET", f"{PREFIX}/status", None),
    "start": ("POST", f"{PREFIX}/start", None),
    "stop": ("POST", f"{PREFIX}/stop", None),
    "enable": ("PUT", f"{PREFIX}/config", {"enabled": True}),
    "disable": ("PUT", f"{PREFIX}/config", {"enabled": False}),
    "retry": ("POST", f"{PREFIX}/retry", None),
}


def request_for(verb: str) -> Request:
    return VERBS[verb]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coverage_drain_cli", description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=sorted(VERBS))
    args = parser.parse_args(argv)
    method, path, body = request_for(args.verb)
    base_url = resolve_backend_base_url().base_url
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        response = client.request(method, f"{base_url}{path}", json=body)
    sys.stdout.write(json.dumps(response.json(), indent=2) + "\n")
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
