"""Detect drift between the KPI map and the spec/thresholds it claims to track.

Split out of kpi_scorecard.py (Thu 10 Sep 2026) to keep that module under the
600-line file-size ratchet; these functions are a cohesive concern of their
own - "does the map still agree with the spec it was built from" - separate
from scoring a reading against a threshold.

Two drift shapes, deliberately different in how strict they are:

- `drift`/`budget_drift` compare id sets and cell TEXT verbatim. A spec
  changing prose without renaming a scenario is exactly the case id drift
  would miss, so budget_drift catches it without parsing meaning out of the
  prose - it forces a human to re-derive any number deliberately.
- `threshold_drift` closes the gap budget_drift cannot: a numeric
  budget/acceptable/breaking edited while the cell text it was derived from
  stays untouched leaves no textual signal at all, and would otherwise still
  ship a verdict against a threshold the spec no longer states.
"""

from __future__ import annotations

import math
import re

_SCENARIO_ROW = re.compile(r"^\|\s*(S\d+)\s*\|", re.MULTILINE)
_PERCENTILE_LABEL = re.compile(r"p\d{2,3}")
_CELL_NUMBER = re.compile(r"(-?\d+(?:\.\d+)?)\s*(ms|s|%)?")


def spec_scenario_ids(spec_text: str) -> list[str]:
    """Scenario ids as the spec itself declares them, in table order.

    Only the id column is parsed. Parsing the whole table would couple this
    tool to the spec's prose formatting, which changes often; the id column is
    the one part that cannot change without the scenario set changing.
    """
    seen: list[str] = []
    for match in _SCENARIO_ROW.finditer(spec_text):
        sid = match.group(1)
        if sid not in seen:
            seen.append(sid)
    return seen


def drift(kpi_map: dict, spec_ids: list[str]) -> tuple[list[str], list[str]]:
    """Scenarios the spec has and the map lacks, and the reverse."""
    mapped = list(kpi_map["scenarios"].keys())
    return ([s for s in spec_ids if s not in mapped], [s for s in mapped if s not in spec_ids])


def spec_budget_cells(spec_text: str) -> dict[str, dict[str, str]]:
    """The Target, Acceptable and Breaking cells of each scenario row, verbatim.

    Splitting a markdown row on the pipe is robust enough while the table keeps
    its shape, and a row whose shape changed is skipped rather than guessed at,
    which surfaces as a drift finding because its cells will not match.
    """
    cells: dict[str, dict[str, str]] = {}
    for line in spec_text.splitlines():
        if not _SCENARIO_ROW.match(line):
            continue
        parts = [c.strip() for c in line.split("|")[1:-1]]
        if len(parts) < 6:
            continue
        cells.setdefault(
            parts[0], {"target": parts[3], "acceptable": parts[4], "breaking": parts[5]}
        )
    return cells


def budget_drift(kpi_map: dict, spec_text: str) -> list[str]:
    """Budget text in the map that no longer matches the spec it was copied from.

    The map duplicates every threshold as a machine-readable number, so the
    spec can change a Target without renaming anything and the tool would go on
    scoring against the stale copy. Comparing the spec's cell TEXT verbatim
    catches any such edit without parsing prose into numbers, and forces a
    human to re-derive the numbers deliberately.
    """
    found = spec_budget_cells(spec_text)
    problems: list[str] = []
    for sid, cfg in kpi_map["scenarios"].items():
        recorded = cfg.get("spec_cells")
        if not recorded:
            problems.append(
                f"{sid}: the map records no spec_cells, so budget drift cannot be detected for it"
            )
            continue
        current = found.get(sid)
        if current is None:
            problems.append(f"{sid}: no parseable budget row found in the spec")
            continue
        problems.extend(
            f"{sid} {column}: spec now reads {current[column]!r}, "
            f"the map was built against {recorded.get(column)!r}"
            for column in ("target", "acceptable", "breaking")
            if str(recorded.get(column, "")) != current[column]
        )
    return problems


def _threshold_matches_cell(threshold: float, unit: str, cell: str) -> bool | None:
    """Does the FIRST number in `cell` assert `threshold` (recorded in `unit`)?

    Returns None when the column asserts nothing checkable: no number is
    present, or the first number's unit does not convert to `unit`'s family.
    Only the first number is read, never a later one in the same cell - spec
    prose routinely names a second, unrelated number afterward (a sampling
    window, a denominator, e.g. S4's "<5% dropped frames per 10s window"),
    and matching against whichever number happens to convert would let that
    window size stand in for a threshold it was never meant to describe.

    A leading percentile label (p95, p50, ...) is stripped first so a KPI's
    own percentile marker is never read as its threshold value.
    """
    stripped = _PERCENTILE_LABEL.sub("", cell)
    match = _CELL_NUMBER.search(stripped)
    if match is None:
        return None
    value = float(match.group(1))
    cell_unit = match.group(2)
    wants_ms = "ms" in unit.lower()
    wants_pct = "%" in unit or "pct" in unit.lower()
    if cell_unit == "ms" and not wants_ms:
        return None
    if cell_unit == "s":
        if wants_ms:
            value *= 1000
        elif wants_pct:
            return None
    if cell_unit == "%" and not wants_pct:
        return None
    return math.isclose(value, threshold, rel_tol=1e-9, abs_tol=1e-9)


def threshold_drift(kpi_map: dict) -> list[str]:
    """The map's own scoring thresholds must match the spec cell they claim to
    score against, not just its prose (budget_drift's job above).

    budget_drift catches the spec's cell TEXT changing under the map. It
    cannot catch the reverse: a numeric `budget`/`acceptable`/`breaking`
    edited (typo, stale copy) while the cell text it was derived from stays
    untouched, which leaves no textual signal for budget_drift to see and
    still ships a PASS against a threshold the owning spec no longer states.

    Scoped to scenario-level fields only: a per-KPI `required` override
    (S2/S4/S7's secondary KPI) shares its one spec_cells column with the
    scenario's own primary threshold, so no per-column text can say which of
    the two numbers in that cell belongs to it - checking it would mean
    guessing, not verifying. `acceptable`/`breaking` equal to `budget` is a
    self-consistency shorthand, not an independent claim, so it is skipped
    rather than double-counted.

    A column whose cell asserts nothing this map's unit can compare against
    is skipped, not flagged - S4's own target cell ("p95 frame delta <=
    display refresh interval") names no number by design, per its
    `missing_kpi` note: the 16.7/33.3ms bands are provisional stand-ins for a
    refresh-relative bound that has no fixed figure to check against yet.
    """
    problems: list[str] = []
    for sid, cfg in kpi_map["scenarios"].items():
        cells = cfg.get("spec_cells")
        if not cells:
            continue
        unit = str(cfg.get("unit", ""))
        budget = cfg.get("budget")
        acceptable = cfg.get("acceptable")
        breaking = cfg.get("breaking")
        for column, value in (
            ("target", budget),
            ("acceptable", acceptable),
            ("breaking", breaking),
        ):
            if value is None:
                continue
            if column != "target" and value == budget:
                continue
            cell = str(cells.get(column, ""))
            if _threshold_matches_cell(float(value), unit, cell) is False:
                problems.append(
                    f"{sid} {column}: map records {value!r}, not found in spec cell {cell!r}"
                )
    return problems
