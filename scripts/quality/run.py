"""Orchestrate the stretch-quality run: fetch, render, analyse, report.

    just stretch-quality

Every stage is separately runnable so a slow render need not be repeated to
re-render a report. Nothing here swallows an exit status: each stage's return
code is checked and propagated.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PLANS = Path(__file__).resolve().parent / "plans"
FRONTEND = REPOSITORY_ROOT / "apps/webui/frontend"
DEFAULT_OUT = REPOSITORY_ROOT / "ops/quality/stretch"
STAGES = ("fetch", "render", "analyse", "report")


def _run(command: list[str], cwd: Path) -> None:
    print(f"\n$ {' '.join(command)}\n  (cwd {cwd})", flush=True)
    result = subprocess.run(command, cwd=cwd, check=False)
    if result.returncode != 0:
        raise SystemExit(f"[ERROR] stage failed with exit {result.returncode}: {' '.join(command)}")


def build_plan(
    work_dir: Path,
    sources_path: Path,
    fixture_ids: list[str] | None,
    arm_ids: list[str] | None = None,
) -> Path:
    """Fuse fixtures, arms, grid and fetched sources into one render plan."""
    fixtures = json.loads((PLANS / "fixtures.json").read_text(encoding="utf-8"))["fixtures"]
    arms = json.loads((PLANS / "arms.json").read_text(encoding="utf-8"))["arms"]
    if arm_ids is not None:
        arms = [arm for arm in arms if str(arm["id"]) in arm_ids]
        missing = set(arm_ids) - {str(arm["id"]) for arm in arms}
        if missing:
            raise SystemExit(f"[ERROR] unknown arm(s): {sorted(missing)}")
    grid = json.loads((PLANS / "grid.json").read_text(encoding="utf-8"))["conditions"]
    sources = {
        str(entry["fixture_id"]): entry
        for entry in json.loads(sources_path.read_text(encoding="utf-8"))["sources"]
    }

    planned: list[dict[str, object]] = []
    for fixture in fixtures:
        fixture_id = str(fixture["id"])
        if fixture_ids is not None and fixture_id not in fixture_ids:
            continue
        source = sources.get(fixture_id)
        if source is None or source["status"] != "ok":
            # A missing fixture is reported MISSING, never quietly swapped.
            print(f"[MISSING] {fixture_id}: {source['detail'] if source else 'not fetched'}")
            continue
        planned.append({**fixture, "source_path": str(Path(str(source["path"])).resolve())})

    if not planned:
        raise SystemExit("[ERROR] no fixtures available; refusing to render an empty grid")

    plan = {
        "sample_rate_hz": 44_100,
        "out_dir": str(work_dir),
        "fixtures": planned,
        "arms": arms,
        "conditions": grid,
    }
    plan_path = work_dir / "plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"[plan] {len(planned)} fixtures x {len(grid)} conditions x {len(arms)} arms "
        f"= {len(planned) * len(grid) * len(arms)} cells -> {plan_path}"
    )
    return plan_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8685", help="lane daemon base URL")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--fixtures", nargs="*", default=None, help="restrict to these fixture ids")
    parser.add_argument("--arms", nargs="*", default=None, help="restrict to these arm ids")
    parser.add_argument("--stages", nargs="*", default=list(STAGES), choices=STAGES)
    args = parser.parse_args(argv)

    work_dir = args.out_dir / "work"
    sources_path = work_dir / "sources.json"
    manifest_path = work_dir / "renders.json"
    results_path = args.out_dir / "results.json"
    report_path = args.out_dir / "report.md"
    python = [sys.executable, "-m"]

    if "fetch" in args.stages:
        _run(
            [
                *python,
                "scripts.quality.fetch_fixtures",
                "--base-url",
                args.base_url,
                "--fixtures",
                str(PLANS / "fixtures.json"),
                "--cache-dir",
                str(work_dir / "sources"),
                "--out",
                str(sources_path),
            ],
            REPOSITORY_ROOT,
        )

    if "render" in args.stages:
        plan_path = build_plan(work_dir, sources_path, args.fixtures, args.arms)
        print(f"\n$ playwright stretch-quality full grid (plan {plan_path})", flush=True)
        result = subprocess.run(
            [
                "pnpm",
                "exec",
                "playwright",
                "test",
                "--config",
                "tests/e2e/playwright.stretch-quality.config.ts",
            ],
            cwd=FRONTEND,
            check=False,
            env={**os.environ, "STRETCH_QUALITY_PLAN": str(plan_path)},
        )
        if result.returncode != 0:
            raise SystemExit(f"[ERROR] render stage failed with exit {result.returncode}")

    if "analyse" in args.stages:
        _run(
            [
                *python,
                "scripts.quality.analyse",
                "--manifest",
                str(manifest_path),
                "--out",
                str(results_path),
            ],
            REPOSITORY_ROOT,
        )

    if "report" in args.stages:
        _run(
            [
                *python,
                "scripts.quality.report",
                "--results",
                str(results_path),
                "--out",
                str(report_path),
            ],
            REPOSITORY_ROOT,
        )

    print(f"\n[OK] stretch-quality run complete -> {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
