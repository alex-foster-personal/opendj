"""Scored per-surface code-quality rubric CLI (issue #389)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts.quality_rubric_model import (
        DEFAULT_RUBRIC,
        SURFACE_IDS,
        Rubric,
        load_rubric,
    )
    from scripts.quality_rubric_probes import run_probe
    from scripts.quality_rubric_score import score_dimension, sort_findings
    from scripts.sparse_worktree import require_materialized_under
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.quality_rubric") from None
    raise

REPO = Path(__file__).resolve().parent.parent


def _git_head() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _resolve_surface_root(
    rubric: Rubric, surface_id: str, root_override: Path | None
) -> tuple[Path, str]:
    if surface_id not in rubric.surfaces:
        raise ValueError(f"unknown surface: {surface_id}")
    declared = rubric.surfaces[surface_id]
    if root_override is not None:
        resolved = root_override.resolve()
        display_root = str(root_override)
    else:
        resolved = (REPO / declared).resolve()
        display_root = declared
    # A surface whose every file is skip-worktree is absent from a sparse tree (OPS-45).
    require_materialized_under(REPO, resolved, purpose=f"rubric surface {surface_id}")
    if not resolved.is_dir():
        raise FileNotFoundError(f"surface root is not a directory: {resolved}")
    return resolved, display_root


def score_surface(
    rubric: Rubric,
    surface_id: str,
    *,
    root_override: Path | None = None,
) -> dict[str, Any]:
    surface_root, display_root = _resolve_surface_root(rubric, surface_id, root_override)
    dimension_results: list[dict[str, Any]] = []
    for dimension in rubric.dimensions_for_surface(surface_id):
        raw_findings = run_probe(dimension.probe, surface_root, REPO)
        findings = sort_findings(raw_findings)
        dimension_results.append(
            {
                "id": dimension.id,
                "score": score_dimension(dimension, findings),
                "max": 5,
                "findings": findings,
            }
        )
    return {
        "id": surface_id,
        "root": display_root,
        "dimensions": dimension_results,
    }


def build_report(
    rubric: Rubric,
    surface_ids: list[str],
    *,
    root_overrides: dict[str, Path] | None = None,
) -> dict[str, Any]:
    overrides = root_overrides or {}
    surfaces: list[dict[str, Any]] = []
    for surface_id in surface_ids:
        surfaces.append(
            score_surface(
                rubric,
                surface_id,
                root_override=overrides.get(surface_id),
            )
        )
    return {
        "rubric_version": rubric.version,
        "rubric_path": rubric.path.relative_to(REPO).as_posix(),
        "scored_at": datetime.now(UTC).isoformat(),
        "commit": _git_head(),
        "surfaces": surfaces,
    }


def _print_human_report(report: dict[str, Any]) -> None:
    for surface in report["surfaces"]:
        print(f"[rubric] surface {surface['id']} root={surface['root']}")
        print("[rubric] dimension | score | findings")
        for dimension in surface["dimensions"]:
            count = len(dimension["findings"])
            print(f"[rubric] {dimension['id']} | {dimension['score']}/{dimension['max']} | {count}")
        for dimension in surface["dimensions"]:
            for finding in dimension["findings"]:
                code = finding.get("code") or "-"
                href = finding.get("href") or finding.get("detail") or ""
                print(
                    f"[rubric] finding {code} {finding.get('class')} "
                    f"{finding.get('path')} {href}".rstrip()
                )


def cmd_validate_rubric(rubric_path: Path) -> int:
    rubric = load_rubric(rubric_path)
    print(f"[rubric] OK {rubric.path.relative_to(REPO)} version {rubric.version}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    rubric_path = Path(args.rubric)
    if not rubric_path.is_file():
        print(f"[rubric] missing rubric: {rubric_path}", file=sys.stderr)
        return 2
    try:
        rubric = load_rubric(rubric_path)
    except Exception as exc:
        print(f"[rubric] rubric load failed: {exc}", file=sys.stderr)
        return 2

    if args.all:
        if args.root:
            print("[rubric] --root cannot be used with --all", file=sys.stderr)
            return 2
        surface_ids = list(SURFACE_IDS)
        root_overrides: dict[str, Path] = {}
    else:
        if args.surface not in SURFACE_IDS:
            print(f"[rubric] unknown surface: {args.surface}", file=sys.stderr)
            return 2
        surface_ids = [args.surface]
        root_overrides = {}
        if args.root:
            root_overrides[args.surface] = Path(args.root).resolve()

    try:
        report = build_report(rubric, surface_ids, root_overrides=root_overrides)
    except FileNotFoundError as exc:
        print(f"[rubric] {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"[rubric] {exc}", file=sys.stderr)
        return 2

    payload = json.dumps(report, indent=2, sort_keys=False)
    payload += "\n"
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")
    if args.json:
        sys.stdout.write(payload)
    else:
        _print_human_report(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quality_rubric",
        description="Score repo surfaces against the evidence-anchored code-quality rubric.",
    )
    parser.add_argument(
        "--rubric",
        default=str(DEFAULT_RUBRIC.relative_to(REPO)),
        help="Path to rubric YAML (default: ops/quality/rubric/v1.yaml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate-rubric", help="Validate rubric YAML against schema.json")

    score = sub.add_parser("score", help="Score one or all surfaces")
    score.add_argument("--surface", choices=SURFACE_IDS, help="Surface id to score")
    score.add_argument("--all", action="store_true", help="Score all four surfaces")
    score.add_argument("--root", help="Override surface root (for fixtures)")
    score.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    score.add_argument("--out", help="Write JSON report to PATH")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    rubric_path = Path(args.rubric)
    if not rubric_path.is_absolute():
        rubric_path = REPO / rubric_path

    if args.command == "validate-rubric":
        if not rubric_path.is_file():
            print(f"[rubric] missing rubric: {rubric_path}", file=sys.stderr)
            return 2
        try:
            return cmd_validate_rubric(rubric_path)
        except Exception as exc:
            print(f"[rubric] validation failed: {exc}", file=sys.stderr)
            return 2

    if args.command == "score":
        if not args.all and not args.surface:
            print("[rubric] score requires --surface or --all", file=sys.stderr)
            return 2
        return cmd_score(args)

    print(f"[rubric] unknown command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
