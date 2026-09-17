"""``python -m apps.smartlists.cli.create`` -- create a smartlist.

Documented agent path: ``opendj api POST /api/v1/smartlists --json '{"name":"...","rule":{...}}'``.
This module writes a local state.db (tests / offline).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from apps.shared.smartlists import SmartlistRuleError, validate_rule
from apps.smartlists.repo import SmartlistsRepoError

from ._common import build_repo


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.smartlists.cli.create",
        description=(
            "Create a smartlist from a JSON rule file. "
            "Documented agent path: opendj api POST /api/v1/smartlists "
            '--json \'{"name":"...","rule":{...}}\'. '
            "This module writes a local state.db (tests / offline)."
        ),
    )
    p.add_argument("--name", required=True)
    p.add_argument("--rule", required=True, type=Path,
                   help="path to a JSON file containing the rule AST")
    p.add_argument("--order-by", default="added_date desc")
    p.add_argument("--db", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rule = json.loads(args.rule.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: could not read rule file: {exc}", file=sys.stderr)
        return 2
    try:
        validate_rule(rule)
    except SmartlistRuleError as exc:
        print(f"error: invalid rule: {exc}", file=sys.stderr)
        return 2
    repo, conn = build_repo(args.db)
    try:
        try:
            row = repo.create(args.name, rule, order_by=args.order_by)
        except SmartlistsRepoError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"created smartlist {row.name!r} (id={row.id})")
        print(f"  referenced_fields={sorted(row.referenced_fields)}")
        print(f"  order_by={row.order_by!r}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
