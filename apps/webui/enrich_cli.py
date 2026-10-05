"""CLI twin of the enrich-on-open card's HTTP surface.

    python -m apps.webui.enrich_cli summary
    python -m apps.webui.enrich_cli decide --lane stems --answer never|ask

Talks to the running engine; prints the JSON response; exits non-zero on any
non-2xx.
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

from apps.webui.coverage_drain_cli import _backend_base_url

PREFIX: str = "/api/v1/enrich"
REQUEST_TIMEOUT_S: float = 60.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="enrich_cli", description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=("summary", "decide"))
    parser.add_argument("--lane")
    parser.add_argument("--answer", choices=("never", "ask"))
    args = parser.parse_args(argv)
    if args.verb == "decide" and not (args.lane and args.answer):
        parser.error("decide needs --lane and --answer")
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        if args.verb == "summary":
            response = client.get(f"{_backend_base_url()}{PREFIX}/summary")
        elif args.verb == "decide":
            response = client.put(
                f"{_backend_base_url()}{PREFIX}/decisions/{args.lane}", json={"answer": args.answer}
            )
    sys.stdout.write(json.dumps(response.json(), indent=2) + "\n")
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
