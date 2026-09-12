"""Post a CI or headless-dmg failure to the one error sink as kind=build.

Stdlib plus apps.shared.telemetry (also stdlib on this path). No network
unless a Sentry client is already active in this process.

Usage:
  python -m scripts.post_build_failure --kind build --message TEXT \\
      --source-site build:CI --sha SHA --host HOST
  python -m scripts.post_build_failure --from-log PATH --sha SHA --host HOST
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from apps.shared.telemetry.sink import (
    event_from_headless_dmg_log,
    post_build_failure,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="post_build_failure")
    parser.add_argument("--kind", default="build")
    parser.add_argument("--message", default="")
    parser.add_argument("--from-log", type=Path)
    parser.add_argument("--source-site", default="build:ci")
    parser.add_argument("--sha")
    parser.add_argument("--host")
    args = parser.parse_args(argv)

    if args.kind != "build":
        print("--kind must be build", file=sys.stderr)
        return 2

    if args.from_log is not None:
        text = args.from_log.read_text(encoding="utf-8")
        parsed = event_from_headless_dmg_log(
            text, host=args.host, build_sha=args.sha
        )
        if parsed is None:
            print("no FAILED verdict")
            return 0
        event = post_build_failure(
            message=parsed.message,
            source_site=parsed.source_site,
            host=args.host,
            build_sha=args.sha,
        )
        print(event.error_id)
        return 0

    if not args.message.strip():
        print("--message or --from-log is required", file=sys.stderr)
        return 2

    event = post_build_failure(
        message=args.message,
        source_site=args.source_site,
        host=args.host,
        build_sha=args.sha,
    )
    print(event.error_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
