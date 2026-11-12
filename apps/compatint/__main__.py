"""`python -m apps.compatint discover-mixxx`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from apps.compatint.coverage import render_discovery_report
from apps.compatint.discover_mixxx import (
    discover_mixxx_from_archive_index,
    fetch_official_archive_index,
)
from apps.compatint.schema import MIXXX_ARCHIVE_BASE_URL


def _load_tags(path: Path | None) -> list[str]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(t, str) for t in payload):
        raise ValueError(f"{path}: tags file must be a JSON list of strings")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Mixxx stable release discovery against the official downloads archive. "
            "Coverage denominators are explicit; discovered lists are never exhaustive."
        )
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    discover = sub.add_parser("discover-mixxx", help="parse archive index and emit corpus JSON")
    discover.add_argument(
        "--archive",
        type=Path,
        help="captured archive index HTML (default: fetch official index)",
    )
    discover.add_argument(
        "--tags",
        type=Path,
        help="JSON list of Git tag names for tag-only gap detection",
    )
    discover.add_argument(
        "--json-out",
        type=Path,
        help="write corpus JSON to this path",
    )
    args = parser.parse_args(argv)

    if args.cmd == "discover-mixxx":
        if args.archive is not None:
            html = args.archive.read_text(encoding="utf-8")
        else:
            html = fetch_official_archive_index(MIXXX_ARCHIVE_BASE_URL)
        tags = _load_tags(args.tags)
        result = discover_mixxx_from_archive_index(html, tags=tags)
        report = render_discovery_report(result)
        sys.stdout.write(report)
        if args.json_out is not None:
            args.json_out.write_text(
                json.dumps(result.to_corpus_dict(), indent=2) + "\n",
                encoding="utf-8",
            )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
