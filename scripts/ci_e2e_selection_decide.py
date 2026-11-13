"""Decision record, reporting and replay for the e2e spec selector."""

from __future__ import annotations

import json
import shutil
import subprocess
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from scripts.ci_e2e_selection_constants import (
    ALL_GATES,
    BUDGET_NAME,
    REASON_ENGINE,
    SCHEMA,
    SEED_MAP,
    SWITCH_ON,
    SWITCH_VARIABLE,
    FullSuite,
    Git,
)
from scripts.ci_e2e_selection_map import Footprints, check_fresh, load_budget, load_map
from scripts.ci_e2e_selection_select import Fit, Selection, fit, select_items, summary_line


@dataclass(frozen=True)
class Record:
    schema: int
    mode: str
    switch: str | None
    reason: str
    changed_files: list[str]
    map_sha: str
    map_built: str
    map_source: str
    universe_specs: int
    universe_seconds: float
    selected: dict[str, list[str]]
    selected_seconds: float
    gates: dict[str, list[str]]
    budget: dict | None
    notes: list[str]


@dataclass(frozen=True)
class Revisions:
    """The merge commit's base and head, whether the map's age and ancestry are checked, and
    the budget context (a Trunk queue draft's head ref starts with `trunk-merge/`)."""

    base: str = "HEAD^1"
    head: str = "HEAD"
    fresh: bool = True
    queue_draft: bool = False


#: The pull_request checkout: GitHub's test merge commit against its base parent.
MERGE_COMMIT = Revisions()


def _git_in(repo: Path) -> Git:
    """`git -C repo ...` by resolved path (AGENTS.md, "Portable by default"); no git is FullSuite."""
    executable = shutil.which("git")
    if executable is None:
        raise FullSuite("git is not on PATH, so the change set and the map's ancestry cannot be read")

    def git(args: Sequence[str]) -> str:
        return subprocess.run([executable, "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout

    return git


def _narrow(
    switch: str | None,
    changed: Sequence[str],
    published: Path | None,
    root: Path,
    git: Git,
    now: datetime,
    revisions: Revisions,
    budget_minutes: float | None,
) -> tuple[Footprints, Selection, Fit]:
    # An unset repo variable reaches the step as an empty string (`${{ vars.X }}`), which is
    # the documented default-on state, not the kill switch (Codex, PR #7948).
    if switch and switch != SWITCH_ON:
        raise FullSuite(f"{SWITCH_VARIABLE}={switch!r} (anything but {SWITCH_ON!r} or empty runs the full suite)")
    if not changed:
        raise FullSuite("the merge commit changes no files; an empty diff is unmeasured")
    footprints = load_map(published, root / SEED_MAP)
    config = load_budget(root / BUDGET_NAME)
    if revisions.fresh:
        # The merge commit, not the base: a map built on main is behind both, and the
        # committed seed is built on this PR's own branch, which only the head contains.
        check_fresh(footprints, now, revisions.head, git)
    selection = select_items(changed, footprints, root=root, base=revisions.base, head=revisions.head, git=git)
    context = "queue_draft" if revisions.queue_draft else "pr_head"
    budget = config.for_context(revisions.queue_draft) if budget_minutes is None else budget_minutes
    return footprints, selection, fit(selection, config, budget, context)


def decide(
    switch: str | None,
    changed: Sequence[str],
    *,
    published: Path | None,
    root: Path,
    git: Git,
    now: datetime,
    revisions: Revisions = MERGE_COMMIT,
    budget_minutes: float | None = None,
) -> Record:
    """The whole decision as a record; never raises for a selection problem (that is FULL)."""
    footprints: Footprints | None = None
    budget: dict | None = None
    try:
        try:
            footprints, selection, fitted = _narrow(
                switch, changed, published, root, git, now, revisions, budget_minutes
            )
        except FullSuite:
            raise
        except Exception as exc:
            raise FullSuite("the selection could not be computed: " + traceback.format_exc()) from exc
        mode = "affected"
        engine = f" ({REASON_ENGINE}: {selection.engine})" if selection.engine else ""
        reason = (
            f"{len(selection.specs)} spec(s) and {len(selection.gates)} gate(s) selected from "
            f"{footprints.source}{engine}; {summary_line(fitted)}"
        )
        selected = {name: selection.specs[name].reasons for name in sorted(selection.specs)}
        gates = {name: selection.gates[name].reasons for name in sorted(selection.gates)}
        budget, notes = fitted.as_record(), selection.notes
    except FullSuite as full:
        mode, reason, selected, gates, notes = "full", str(full), {}, {}, []
        if footprints is None:
            footprints = _map_if_loadable(published, root)
    universe = len(footprints.specs) if footprints else 0
    universe_seconds = sum(footprints.seconds(s) for s in footprints.specs) if footprints else 0.0
    selected_seconds = sum(footprints.seconds(s) for s in selected) if footprints else 0.0
    return Record(
        SCHEMA,
        mode,
        switch,
        reason,
        list(changed),
        footprints.sha if footprints else "",
        footprints.built.isoformat() if footprints else "",
        footprints.source if footprints else "",
        universe,
        round(universe_seconds, 1),
        selected,
        round(selected_seconds, 1),
        gates,
        budget,
        notes,
    )


def _map_if_loadable(published: Path | None, root: Path) -> Footprints | None:
    """The map for the record's universe figures when FULL was decided before or without one."""
    try:
        return load_map(published, root / SEED_MAP)
    except (FullSuite, OSError, ValueError):
        return None


def gates_output(record: Record) -> str:
    """` a b ` with a space on both ends, so a step's `contains(..., ' a ')` is exact."""
    names = ALL_GATES if record.mode == "full" else tuple(record.gates)
    return f" {' '.join(names)} " if names else " "


def report_lines(record: Record) -> list[str]:
    lines = [f"[e2e-selection] mode={record.mode}: {record.reason}"]
    if record.mode == "affected":
        share = record.universe_specs and len(record.selected) / record.universe_specs
        lines.append(
            f"[e2e-selection] selected {len(record.selected)} of {record.universe_specs} root specs "
            f"({share:.0%}), {record.selected_seconds:.0f} of {record.universe_seconds:.0f} recorded seconds; "
            f"gates: {', '.join(record.gates) or 'none'}"
        )
        lines.extend(f"[e2e-selection]   {spec}  <- {'; '.join(reasons)}" for spec, reasons in record.selected.items())
        lines.extend(
            f"[e2e-selection]   gate {gate}  <- {'; '.join(reasons)}" for gate, reasons in record.gates.items()
        )
        if record.budget:
            lines.extend(
                f"[e2e-selection]   dropped by the budget: {d['item']} (overlap {d['overlap']}, {d['seconds']:g} s)"
                for d in record.budget["dropped"]
            )
    lines.extend(f"[e2e-selection]   note: {note}" for note in record.notes)
    return lines


def summary_markdown(record: Record) -> str:
    rows = [
        "### Root Playwright spec selection (TEST-BUDGET round 3)",
        "",
        "| | |",
        "|:--|:--|",
        f"| mode | `{record.mode}` |",
        f"| reason | {record.reason.splitlines()[0]} |",
        f"| footprint map | `{record.map_sha[:9]}` built {record.map_built} ({record.map_source}) |",
        f"| selected specs | {len(record.selected)} of {record.universe_specs} |",
        f"| recorded seconds | {record.selected_seconds:.0f} of {record.universe_seconds:.0f} |",
        f"| named gates | {', '.join(record.gates) if record.mode == 'affected' else 'all'} |",
        "",
    ]
    if record.mode == "affected":
        rows.append("<details><summary>selected specs and gates</summary>\n")
        rows += [f"- `{spec}`: {'; '.join(reasons)}" for spec, reasons in record.selected.items()]
        rows += [f"- gate `{gate}`: {'; '.join(reasons)}" for gate, reasons in record.gates.items()]
        if record.budget and record.budget["dropped"]:
            rows.append("\nDropped by the budget:\n")
            rows += [f"- `{d['item']}` (overlap {d['overlap']}, {d['seconds']:g} s)" for d in record.budget["dropped"]]
        rows.append("\n</details>")
    return "\n".join(rows) + "\n"


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
