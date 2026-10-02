"""Post a CI or headless-dmg failure to the one error sink as kind=build.

Stdlib plus apps.shared.telemetry (also stdlib on this path). No network
unless a Sentry client is already active in this process.

Usage:
  python -m scripts.post_build_failure --kind build --message TEXT \\
      --source-site build:CI --sha SHA --host HOST
  python -m scripts.post_build_failure --from-log PATH --sha SHA --host HOST
  python -m scripts.post_build_failure --batch-file ci-sink-failures.json --host HOST

--batch-file posts the failed completions the CI error sink batch (ci-error-sink.yml) selected,
each once: a record already in the sink is skipped, so overlapping passes
re-read safely, and every record is read back after posting, because append_sink never
raises and a lost write must fail the step rather than pass it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from apps.shared.telemetry.sink import (
    event_from_headless_dmg_log,
    post_build_failure,
    sink_files,
)


def _sink_key(record: dict[str, Any]) -> str:
    return f" run={record['run_id']} attempt={record['run_attempt']} "


def _build_message(line: str) -> str:
    """The message of a kind=build row; "" for any other row or a torn line."""
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return ""
    if isinstance(row, dict) and row.get("kind") == "build":
        return str(row.get("message", ""))
    return ""


def _keys_in_sink(keys: set[str]) -> set[str]:
    """Keys held by a kind=build row's message; the raw text elsewhere does not count."""
    found: set[str] = set()
    for path in sink_files():
        if not path.is_file():
            continue
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if any(key in line for key in keys - found):
                    message = _build_message(line)
                    found.update(key for key in keys - found if key in message)
    return found


def post_batch(batch_file: Path, host: str | None) -> int:
    records = {_sink_key(record): record for record in json.loads(batch_file.read_text())}
    already = _keys_in_sink(set(records))
    for key, record in records.items():
        if key in already:
            continue
        post_build_failure(
            message=(
                f"CI failed workflow={record['workflow']}{key}"
                f"conclusion={record['conclusion']} url={record['url']} "
                f"sha={record['head_sha']}"
            ),
            source_site=f"build:{record['workflow']}",
            host=host,
            build_sha=record["head_sha"],
        )
    print(
        f"[sink] listed={len(records)} posted={len(records) - len(already)} already={len(already)}"
    )
    missing = sorted(set(records) - _keys_in_sink(set(records)))
    for key in missing:
        print(f"[sink] not in the sink after posting:{key.rstrip()}", file=sys.stderr)
    return 1 if missing else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="post_build_failure")
    parser.add_argument("--kind", default="build")
    parser.add_argument("--message", default="")
    parser.add_argument("--from-log", type=Path)
    parser.add_argument("--batch-file", type=Path)
    parser.add_argument("--source-site", default="build:ci")
    parser.add_argument("--sha")
    parser.add_argument("--host")
    args = parser.parse_args(argv)

    if args.kind != "build":
        print("--kind must be build", file=sys.stderr)
        return 2

    if args.batch_file is not None:
        return post_batch(args.batch_file, args.host)

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
