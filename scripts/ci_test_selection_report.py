"""Human-readable reports and JUnit skipped-module checks."""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree

from scripts.ci_test_budget import summary_line
from scripts.ci_test_selection_decide import Record
from scripts.ci_test_selection_event import DEMOTE_VARIABLE, QUEUE_SWITCH_VARIABLE, SWITCH_VARIABLE


def _fit_of(record: Record):
    from scripts.ci_test_budget import Fit

    return Fit(**(record.budget or {}))


def shard_mode(record: Record, shard: int) -> str:
    if record.mode == "full":
        return "full"
    return "affected" if record.shards.get(str(shard)) else "empty"


def report_lines(record: Record, shard: int, shards: int) -> list[str]:
    mode = shard_mode(record, shard)
    lines = [f"PR TEST SELECTION: shard {shard} of {shards} runs mode={mode.upper()}"]
    lines.append(f"  reason: {record.reason}")
    lines.append(
        f"  {SWITCH_VARIABLE}={record.switch!r}, {QUEUE_SWITCH_VARIABLE}={record.queue_switch!r}, "
        f"event={record.event}, head={record.head_ref}"
    )
    if record.mode == "affected":
        mine = record.shards.get(str(shard), [])
        lines.append(
            f"  selected {len(record.selected_modules)} of {record.universe_modules} lane module(s) "
            f"for {len(record.changed_paths)} changed path(s), {len(record.declared_modules)} "
            f"declared in the PR body; this shard runs {len(mine)}"
        )
        lines += [f"    {module}" for module in mine]
        if record.demoted:
            lines.append(f"  {len(record.demoted)} weak-link module(s) not run at this head ({DEMOTE_VARIABLE})")
    if record.budget:
        lines.append("  " + summary_line(_fit_of(record)))
    if mode == "empty":
        lines.append(
            "  ZERO affected tests for this shard: it reports success without running pytest. "
            "The nightly FULL suites (ci.yml schedule, full-ci.yml) still run everything on main."
        )
    return lines


def summary_markdown(record: Record, shard: int, shards: int) -> str:
    mode = shard_mode(record, shard)
    rows = [
        f"### pytest fast lane (shard {shard} of {shards}): selection mode **{mode.upper()}**",
        "",
        "| field | value |",
        "|:--|:--|",
        f"| reason | {record.reason} |",
        f"| `{SWITCH_VARIABLE}` / `{QUEUE_SWITCH_VARIABLE}` | `{record.switch}` / `{record.queue_switch}` |",
        f"| event / head | `{record.event}` / `{record.head_ref}` |",
        f"| changed paths | {len(record.changed_paths)} |",
        f"| selected modules (all shards) | {len(record.selected_modules)} of {record.universe_modules} |",
        f"| declared in the PR body (`CI-Tests:`) | {len(record.declared_modules)} |",
        f"| this shard's modules | {len(record.shards.get(str(shard), []))} |",
        f"| weak-link modules not run at this head | {len(record.demoted)} |",
    ]
    if record.budget:
        rows.append(f"| test budget | {summary_line(_fit_of(record))} |")
    rows.append("")
    if mode == "empty":
        rows.append(
            "Zero affected tests for this shard, so pytest did not start. The nightly full "
            "suites (ci.yml schedule, full-ci.yml) still run everything on main."
        )
    elif mode == "affected":
        rows.append("<details><summary>this shard's modules</summary>\n")
        rows += [f"- `{m}`" for m in record.shards[str(shard)]]
        rows.append("\n</details>")
    return "\n".join(rows) + "\n"


# ---------------------------------------------------------------------------
# JUnit evidence for exit 5
# ---------------------------------------------------------------------------


def _dotted(module_path: str) -> str:
    return module_path.removesuffix(".py").replace("/", ".")


def junit_all_skipped(report: Path, modules: list[str]) -> bool:
    """True only when EVERY selected module skipped itself at module level, and nothing else ran.

    pytest exits 5 when it collects no test items. A module that skips itself at module level
    (`pytest.skip(..., allow_module_level=True)`) collects none and leaves one testcase named
    by its dotted path with a <skipped> child; a module with no tests leaves nothing at all.
    So exit 5 is excused only when each selected module has that skipped testcase and the
    report holds no other outcome, which is what the full lane reports green for them.
    """
    if not modules:
        return False
    cases = ElementTree.parse(report).getroot().findall(".//testcase")
    if not cases or not all(case.find("skipped") is not None for case in cases):
        return False
    skipped_modules = {case.get("name", "") for case in cases if not case.get("classname")}
    return all(_dotted(module) in skipped_modules for module in modules)
