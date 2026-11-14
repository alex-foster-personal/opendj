"""Which root Playwright specs and named gates a pull request HEAD runs in the e2e gate
(TEST-BUDGET round 3).

The "Root Playwright suite (real engine + Vite)" step of `e2e.yml`'s gate ran every spec
the root config collects on every in-scope PR, about 9 of the gate's 22 minutes, and the
named gate steps around it (rekordbox, comment-hotkey, autoplay, headphone, library jobs,
preflight, webkit, deck-load smoke, stretch, master12, onboarding, the two Cmd K gates) ran
whatever changed. A
static route-to-source closure cannot narrow it: 199 of about 270 `page.goto` calls target
`/performance`, whose page reaches 759 of the 1172 source files. So the narrowing is test
impact analysis from MEASURED footprints: a map of which `src/` files each spec and each
named gate fetched from the dev server during a full run
(`apps/webui/frontend/vite-e2e-footprints.ts`, `tests/e2e/support/footprint-reporter.ts`,
built and published by `scripts/e2e_footprints_refresh.py`), ranked and fitted into a
runner-minute budget (`ci/e2e-budget.json`; the maintainer, Fri 30 Oct 2026: a hard cap per run,
the e2e share about 4 minutes on a frontend PR). Decision record:
(private path); rule: (private path).

Mode, decided once in the scope job and read by the gate's steps:

    full      the root step runs today's invocation and every named gate runs, unchanged.
    affected  the root step appends the selected spec paths as positional arguments, and a
              named gate step runs only when its name is in `e2e_gates`.

Candidates, each with a reason (the record names every rule that fired):
  - footprint overlap: a changed `apps/webui/frontend/src/` file is in the spec's or gate's
    footprint; the overlap COUNT (changed files it executed) is its rank.
  - changed spec: a changed `tests/e2e/*.spec.ts` runs itself; a gate whose collected specs
    include it runs. Strong: never dropped by the budget.
  - always run: the smoke floor `ci/e2e-always-run.txt`. Strong.
  - unmeasured: a spec the map saw fail or time out, a spec on disk the map never collected,
    a dev-server gate absent from the map, and the static gates (webkit, deck-load smoke,
    stretch, master12, onboarding, the two Cmd K gates, which no request log can footprint) when any file under
    the frontend changed. Ranked after every overlap; a static gate is priced by static_gates_min so an unmeasured gate is never free.

The budget (`fit`): planned minutes = root overhead + root spec seconds, plus per kept gate
its overhead + seconds. Strong items are priced first; the weak candidates are taken in
rank order (overlap descending, recorded seconds ascending) while they fit; the rest are
listed under `budget.dropped` so an escape can be attributed. Strong items alone over the
budget run anyway and the record says `over_budget`.

Falls back to FULL, never to a smaller run, and names the cause, when:
  - the switch repo variable `CI_E2E_SPEC_SELECTION` is set to anything but `affected`
    (unset or empty means on; `full` is the written kill switch);
  - a changed path is global (`FULL_PATTERNS`: the e2e support and fixture trees, any
    Playwright or Vite config, the frontend's package manifest or lockfile, the engine
    the suite boots, the map, the budget, this selector), or is any other non-spec file
    under the e2e tree (an allow list a spec reads has no footprint naming its reader);
  - a changed Python module is not in the import graph at all (deleted, or outside a
    package); a module the engine imports, a file under `apps/webui/server/`, or the
    Python dependency manifest or lock the gate installs from, is instead an ENGINE
    change: every spec and footprinted gate becomes a candidate with no overlap, so the
    budget takes the cheapest (the maintainer's hard cap);
  - a changed `src/` file is in no footprint AND existed on the base but not at the map's
    SHA (added after the map was built: the map is stale for it);
  - the map is missing, malformed, older than `MAX_MAP_AGE_DAYS`, or built at a SHA that
    is not an ancestor of the merge commit; the budget config is missing or malformed;
  - the selection cannot be computed for any reason at all.

Usage (the scope job of .github/workflows/e2e.yml):
    python3 -m scripts.ci_e2e_selection select --published MAP --out-dir DIR
    python3 -m scripts.ci_e2e_selection replay --merges 150 [--map MAP] [--budget-minutes 4]

`select` prints exactly one line on stdout, the mode, writes DIR/e2e-selection.json and
DIR/e2e-specs.txt, and appends `e2e_mode`, `e2e_specs` and `e2e_gates` to $GITHUB_OUTPUT.
Everything for humans goes to stderr and the job summary. Standard library only: the
scope job runs on the runner image's own `python3`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from scripts.affected_tests import REPO
from scripts.ci_e2e_selection_constants import (  # noqa: F401 re-exported public surface
    ALL_GATES,
    ALWAYS_RUN_NAME,
    BUDGET_NAME,
    FRONTEND,
    GATES_AFTER_THE_MAP,
    RECORD_NAME,
    SCHEMA,
    SEED_MAP,
    SPECS_NAME,
    STATIC_GATES,
    SWITCH_VARIABLE,
    FullSuite,
    Git,
)
from scripts.ci_e2e_selection_decide import (  # noqa: F401 re-exported public surface
    MERGE_COMMIT,
    Record,
    Revisions,
    decide,
    gates_output,
    replay,
    report_lines,
    summary_markdown,
)
from scripts.ci_e2e_selection_decide import _git_in
from scripts.ci_e2e_selection_map import (  # noqa: F401 re-exported public surface
    BudgetConfig,
    Footprints,
    load_budget,
    validate_map,
)
from scripts.ci_e2e_selection_select import (  # noqa: F401 re-exported public surface
    Item,
    _cost_min,
    always_run_specs,
    fit,
    select_items,
)
from scripts.ci_required_gate import merge_commit_changed_files


def _append(path_env: str, text: str) -> None:
    target = os.environ.get(path_env)
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)


def _select(arguments: argparse.Namespace) -> int:
    root = Path(arguments.root).resolve()
    changed = merge_commit_changed_files(root)
    queue_draft = os.environ.get("GITHUB_HEAD_REF", "").startswith("trunk-merge/")
    record = decide(
        os.environ.get(SWITCH_VARIABLE),
        changed,
        published=Path(arguments.published) if arguments.published else None,
        root=root,
        git=_git_in(root),
        now=datetime.now(UTC),
        revisions=Revisions(queue_draft=queue_draft),
    )
    out_dir = Path(arguments.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / RECORD_NAME).write_text(json.dumps(asdict(record), indent=2) + "\n", encoding="utf-8", newline="\n")
    specs = " ".join(record.selected) if record.mode == "affected" else ""
    (out_dir / SPECS_NAME).write_text(specs + "\n", encoding="utf-8", newline="\n")
    for line in report_lines(record):
        print(line, file=sys.stderr)
    _append("GITHUB_STEP_SUMMARY", summary_markdown(record))
    _append("GITHUB_OUTPUT", f"e2e_mode={record.mode}\ne2e_specs={specs}\ne2e_gates={gates_output(record)}\n")
    print(record.mode)
    return 0


# ---------------------------------------------------------------------------
# replay: the measurement
# ---------------------------------------------------------------------------


def _percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))] if ordered else 0.0


def _share_row(label: str, values: list[float]) -> str:
    if not values:
        return f"| {label} | n/a |"
    return (
        f"| {label} | median {_percentile(values, 0.5):.0%}, p90 {_percentile(values, 0.9):.0%}, "
        f"mean {sum(values) / len(values):.0%} (n={len(values)}) |"
    )


def _dropped_specs(record: Record) -> int:
    return sum(1 for d in (record.budget or {}).get("dropped", []) if d["item"].startswith("spec "))


def replay(
    root: Path, ref: str, merges: int, published: Path | None, git: Git, now: datetime, budget_minutes: float | None
) -> str:
    """Replay the selector over the last `merges` first-parent merges of `ref`, as a report.

    Measures the steady state: the map is taken as fresh for every merge (`fresh=False`
    skips the age and ancestry checks, which a map built today fails for every past base),
    and the Python import graph is today's. Each merge's change set is its diff against its
    first parent, which is what the scope job sees on the PR's test-merge commit. Only
    merges the e2e scope would have run are counted (`scripts.ci_pr_scope`).
    """
    from scripts.ci_pr_scope import E2E_PATHS_IGNORE, workflow_would_run_for_files

    shas = git(["log", "--merges", "--first-parent", "--format=%H", "-n", str(merges), ref]).split()
    rows: list[Record] = []
    shas_in_scope: list[str] = []
    out_of_scope = 0
    for sha in shas:
        changed = [line for line in git(["diff", "--no-renames", "--name-only", f"{sha}^1", sha]).split("\n") if line]
        if not workflow_would_run_for_files(changed, paths_ignore=E2E_PATHS_IGNORE):
            out_of_scope += 1
            continue
        revisions = Revisions(f"{sha}^1", sha, fresh=False)
        shas_in_scope.append(sha)
        rows.append(
            decide(
                None,
                changed,
                published=published,
                root=root,
                git=git,
                now=now,
                revisions=revisions,
                budget_minutes=budget_minutes,
            )
        )
    universe = max((r.universe_specs for r in rows), default=0) or 1
    narrowed = [r for r in rows if r.mode == "affected"]
    full = [r.reason.splitlines()[0] for r in rows if r.mode == "full"]
    kept_share = [len(r.selected) / universe if r.mode == "affected" else 1.0 for r in rows]
    before_share = [(len(r.selected) + _dropped_specs(r)) / universe if r.mode == "affected" else 1.0 for r in rows]
    seconds_share = [
        r.selected_seconds / r.universe_seconds if r.mode == "affected" and r.universe_seconds else 1.0 for r in rows
    ]
    planned = [r.budget["planned_min"] for r in narrowed if r.budget]
    gates_kept = [len(r.gates) / len(ALL_GATES) if r.mode == "affected" else 1.0 for r in rows]
    over = sum(1 for r in narrowed if r.budget and r.budget["over_budget"])
    causes: dict[str, int] = {}
    for reason in full:
        key = reason.split(" changed: ", 1)[1] if " changed: " in reason else reason.split(":", 1)[0]
        causes[key] = causes.get(key, 0) + 1
    budget_label = f"{budget_minutes:g}" if budget_minutes is not None else "ci/e2e-budget.json"
    lines = [
        f"replayed {len(shas)} first-parent merges of {ref}: {len(rows)} in e2e scope, {out_of_scope} out of scope (no gate run)",
        f"universe: {universe} root specs, {len(ALL_GATES)} named gates; budget {budget_label} runner-min",
        "",
        "| measure (in-scope merges; a FULL merge counts as 100%) | value |",
        "|:--|:--|",
        f"| FULL fraction | {len(full)}/{len(rows)} = {len(full) / len(rows):.0%} |"
        if rows
        else "| FULL fraction | n/a |",
        _share_row("root spec share kept after the budget", kept_share),
        _share_row("root spec share by overlap before the budget", before_share),
        _share_row("root spec share kept, narrowed merges only", [len(r.selected) / universe for r in narrowed]),
        _share_row("recorded-seconds share kept", seconds_share),
        _share_row(f"named gates kept (of {len(ALL_GATES)})", gates_kept),
        (
            f"| planned minutes, narrowed merges | median {_percentile(planned, 0.5):g}, p90 {_percentile(planned, 0.9):g} |"
            if planned
            else "| planned minutes | n/a |"
        ),
        f"| over budget on strong items alone | {over}/{len(narrowed)} |",
        "",
        "FULL causes:",
    ]
    lines += [f"- {count} x {cause}" for cause, count in sorted(causes.items(), key=lambda kv: -kv[1])]
    lines += ["", "per merge (sha, mode, kept (private path), gates, planned min, first reason):"]
    for sha, record in zip(shas_in_scope, rows, strict=True):
        planned_min = record.budget["planned_min"] if record.budget else 0.0
        lines.append(
            f"- {sha[:10]} {record.mode} {len(record.selected)}/{universe} g={len(record.gates)} "
            f"{planned_min:g}min {record.reason.splitlines()[0][:100]}"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select", help="decide the root suite's mode, specs and gates")
    select.add_argument("--published", help="the fetched ci-data-e2e map; absent means the committed seed")
    select.add_argument("--out-dir", required=True)
    select.add_argument("--root", default=str(REPO))
    rerun = commands.add_parser("replay", help="measure the selection over past merges (the map taken as fresh)")
    rerun.add_argument("--merges", type=int, default=150)
    rerun.add_argument("--ref", default="origin/main")
    rerun.add_argument("--map", help="a map to replay with; absent means the committed seed")
    rerun.add_argument("--budget-minutes", type=float, help="override the budget for a sensitivity run")
    rerun.add_argument("--root", default=str(REPO))
    arguments = parser.parse_args(argv)
    if arguments.command == "select":
        return _select(arguments)
    if arguments.command == "replay":
        root = Path(arguments.root).resolve()
        published = Path(arguments.map) if arguments.map else None
        print(
            replay(
                root,
                arguments.ref,
                arguments.merges,
                published,
                _git_in(root),
                datetime.now(UTC),
                arguments.budget_minutes,
            )
        )
        return 0
    parser.error(f"unknown command {arguments.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
