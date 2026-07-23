"""``python -m apps.smartlists.cli.update`` -- replace a smartlist rule."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from apps.shared.smartlists import SmartlistRuleError, validate_rule
from apps.smartlists.repo import SmartlistRevisionConflict, SmartlistsRepoError

from ._common import build_repo, smartlist_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.smartlists.cli.update",
        description="Replace a smartlist rule from a JSON rule file.",
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--rule", required=True, type=Path)
    parser.add_argument(
        "--expected-revision",
        required=True,
        help="Exact revision returned by list --format json.",
    )
    parser.add_argument("--order-by", default=None)
    parser.add_argument("--db", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rule = json.loads(args.rule.read_text())
        validate_rule(rule)
    except (OSError, json.JSONDecodeError, SmartlistRuleError) as exc:
        print(f"error: could not read a valid rule file: {exc}", file=sys.stderr)
        return 2
    repo, conn = build_repo(args.db)
    try:
        row = repo.get_by_name(args.name)
        if row is None:
            print(f"no smartlist named {args.name!r}", file=sys.stderr)
            return 1
        try:
            updated = repo.update_rule(
                row.id,
                rule,
                expected_revision=args.expected_revision,
                order_by=args.order_by,
            )
        except SmartlistRevisionConflict as exc:
            json.dump(
                {
                    "error": "conflict",
                    "message": str(exc),
                    "current": smartlist_json(exc.current),
                    "revision": exc.current_revision,
                },
                sys.stderr,
                sort_keys=True,
            )
            sys.stderr.write("\n")
            return 3
        except SmartlistsRepoError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"updated smartlist {updated.name!r} (id={updated.id})")
        print(f"  referenced_fields={sorted(updated.referenced_fields)}")
        print(f"  order_by={updated.order_by!r}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
