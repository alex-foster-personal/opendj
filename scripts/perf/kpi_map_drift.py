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

_SCENARIO_ROW = re.compile(r"^\|\s*(S\d+|[A-Z]+(?:-[A-Z]+)+)\s*\|", re.MULTILINE)
_PERCENTILE_LABEL = re.compile(r"p\d{2,3}")
_CELL_NUMBER = re.compile(r"(-?\d+(?:\.\d+)?)\s*(ms|s|%)?")


def spec_scenario_ids(spec_text: str) -> list[str]:
    """Scenario ids as the spec itself declares them, in table order.

    Only the id column is parsed. Parsing the whole table would couple this
    tool to the spec's prose formatting, which changes often; the id column is
    the one part that cannot change without the scenario set changing.

    An id is either the numbered `S\\d+` shape (S1..S13) or a hyphenated
    all-caps name (BOOT-LIB), which needs at least one hyphen precisely so it
    is not confused with the `T1`/`T2`/`B1`..`B4` ids used by this same
    document's unrelated Findings and Bet tables further down - those are a
    single letter plus digits, no hyphen, and must stay invisible here.
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
    """The Class, Target, Acceptable and Breaking cells of each scenario row, verbatim.

    Splitting a markdown row on the pipe is robust enough while the table keeps
    its shape, and a row whose shape changed is skipped rather than guessed at,
    which surfaces as a drift finding because its cells will not match. The
    table's own header is "# | Scenario | Class | Target | Acceptable |
    Breaking | Instrumented today?", so `parts[2]` is Class.
    """
    cells: dict[str, dict[str, str]] = {}
    for line in spec_text.splitlines():
        if not _SCENARIO_ROW.match(line):
            continue
        parts = [c.strip() for c in line.split("|")[1:-1]]
        if len(parts) < 6:
            continue
        cells.setdefault(
            parts[0],
            {
                "class": parts[2],
                "target": parts[3],
                "acceptable": parts[4],
                "breaking": parts[5],
            },
        )
    return cells


def class_drift(kpi_map: dict, spec_text: str) -> list[str]:
    """A scenario's Class column reclassified in the spec without its id
    changing, which nothing else here catches: budget_drift compares Target/
    Acceptable/Breaking prose only, and a class change alone can leave every
    one of those cells, and the scenario id, untouched - the stable id is
    exactly what let a P0 reclassified to P2 keep scoring against its old,
    now-wrong severity with no other signal that anything moved.
    """
    found = spec_budget_cells(spec_text)
    problems: list[str] = []
    for sid, cfg in kpi_map["scenarios"].items():
        current = found.get(sid)
        if current is None:
            continue
        recorded = str(cfg.get("class", ""))
        if recorded != current["class"]:
            problems.append(
                f"{sid} class: spec now reads {current['class']!r}, "
                f"the map records {recorded!r}"
            )
    return problems


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


def resolve_required(cfg: dict) -> list[dict]:
    """Normalize each required KPI into its own name/unit/threshold set.

    A plain KPI name (the common shape: one required KPI) inherits the
    scenario's single budget/acceptable/breaking/unit. A dict entry carries
    its own, for a scenario whose required KPIs do not share one threshold -
    S2's visual-feedback KPI budgets at 16ms while its audible KPI budgets at
    30ms, and scoring the visual reading against the audible band is the same
    invented-threshold defect as picking a threshold out of thin air.
    """
    resolved = []
    for entry in cfg.get("required", []):
        if isinstance(entry, str):
            resolved.append(
                {
                    "kpi": entry,
                    "unit": cfg.get("unit", ""),
                    "budget": cfg.get("budget"),
                    "acceptable": cfg.get("acceptable"),
                    "breaking": cfg.get("breaking"),
                    "lower_is_better": cfg.get("lower_is_better", True),
                }
            )
        else:
            resolved.append(entry)
    return resolved


def _unit_wants(unit: str) -> tuple[bool, bool]:
    """(wants_ms, wants_pct) parsed from a KPI's declared unit string."""
    lowered = unit.lower()
    return "ms" in lowered, ("%" in unit or "pct" in lowered)


def _threshold_matches_cell(threshold: float, unit: str, cell: str) -> bool | None:
    """Does the FIRST number in `cell` assert `threshold` (recorded in `unit`)?

    Returns None when the column asserts nothing checkable: no number is
    present, or the first number's unit does not convert to `unit`'s family.
    Only the first number is read, never a later one in the same cell - spec
    prose routinely names a second, unrelated number afterward (a sampling
    window, a denominator, e.g. S4's "<5% dropped frames per 10s window"),
    and matching against whichever number happens to convert would let that
    window size stand in for a threshold it was never meant to describe.

    ms and plain seconds convert into each other in both directions (a
    seconds-based scenario scored against a spec cell stated in milliseconds
    must still be checked, not silently skipped for the unit not matching
    literally). A leading percentile label (p95, p50, ...) is stripped first
    so a KPI's own percentile marker is never read as its threshold value.
    """
    wants_ms, wants_pct = _unit_wants(unit)
    stripped = _PERCENTILE_LABEL.sub("", cell)
    match = _CELL_NUMBER.search(stripped)
    if match is None:
        return None
    value = float(match.group(1))
    cell_unit = match.group(2)
    if cell_unit == "ms":
        if wants_pct:
            return None
        if not wants_ms:
            value /= 1000
    elif cell_unit == "s":
        if wants_pct:
            return None
        if wants_ms:
            value *= 1000
    elif cell_unit == "%" and not wants_pct:
        return None
    return math.isclose(value, threshold, rel_tol=1e-9, abs_tol=1e-9)


_DIRECTION = re.compile(r"(<=|>=|<|>)\s*-?\d")


def _direction_matches(lower_is_better: bool, column: str, cell: str) -> bool | None:
    """Does `cell`'s comparator, immediately before its first number, agree
    with `lower_is_better`?

    Returns None when the cell states no comparator right before a number -
    a prose-only cell (S4's provisional target, S1's qualitative breaking)
    asserts no checkable direction, the same "nothing to compare against"
    case the numeric checks already skip rather than flag.

    `target`/`acceptable` read as an upper bound when lower_is_better (a
    smaller reading is the good one, so the cell says "<=" or "<"), and a
    lower bound otherwise (">=" or ">"). `breaking` is the opposite sense:
    it names the bound a reading must NOT cross, so a lower-is-better
    scenario's breaking cell reads ">" (exceeding an upper ceiling breaks
    it), while a higher-is-better scenario's breaking cell reads "<"
    (falling under a floor breaks it).
    """
    match = _DIRECTION.search(cell)
    if match is None:
        return None
    cell_is_upper_bound = match.group(1).startswith("<")
    if column == "breaking":
        cell_is_upper_bound = not cell_is_upper_bound
    return cell_is_upper_bound == lower_is_better


def _per_kpi_family(unit: str) -> str:
    wants_ms, wants_pct = _unit_wants(unit)
    if wants_pct:
        return "pct"
    if wants_ms:
        return "ms"
    if unit.strip().lower() == "s":
        return "s"
    return "bare"


_FAMILY_TAG = {"ms": "ms", "s": "s", "pct": "%", "bare": None}


def _strict_family_candidates(cell: str, family: str) -> list[float]:
    """Numbers in `cell` whose OWN unit tag literally matches `family`, with
    no cross-unit conversion - "ms" only for a `ms`-tagged number, "pct" only
    for `%`, "bare" only for an untagged number, and so on.

    Conversion is deliberately excluded here, unlike `_threshold_matches_cell`
    above: two required KPIs often share one cell, and the OTHER one's number
    routinely carries a convertible unit that has nothing to do with this KPI
    (S4's acceptable cell states a dropped-frame percentage AND a "10s window"
    duration; converting that window into milliseconds would make it collide
    with the frame-delta KPI's own ms-typed threshold and validate against a
    sample window instead). Restricting a family to its own literal tag keeps
    every match referring to a number that could plausibly BE that KPI's own
    threshold, at the cost of leaving a real edit undetected when the cell
    states its bound in prose with no attached digit at all (S7's
    dropped-keystroke breaking is exactly this: "or dropped keystrokes"
    asserts a zero threshold with no literal 0 in the text - the same class
    of gap as a target cell with no number in it at all).
    """
    stripped = _PERCENTILE_LABEL.sub("", cell)
    wants_tag = _FAMILY_TAG[family]
    return [
        float(raw)
        for raw, cell_unit in _CELL_NUMBER.findall(stripped)
        if (cell_unit or None) == wants_tag
    ]


_CELL_NUMBER_OP = re.compile(r"(<=|>=|<|>)?\s*(-?\d+(?:\.\d+)?)\s*(ms|s|%)?")


def _strict_family_operators(cell: str, family: str) -> list[str | None]:
    """The same rank-by-family walk as `_strict_family_candidates`, returning
    each matched number's OWN leading comparator (or None when it has none)
    instead of its value.

    The comparator immediately in front of a candidate is that candidate's
    own direction, not the cell's first comparator overall - S2's target cell
    states "<=30ms audible, <=16ms visual", and the second KPI's comparator
    must come from beside ITS OWN number, not be borrowed from the first.
    """
    stripped = _PERCENTILE_LABEL.sub("", cell)
    wants_tag = _FAMILY_TAG[family]
    return [
        (op or None)
        for op, raw, cell_unit in _CELL_NUMBER_OP.findall(stripped)
        if (cell_unit or None) == wants_tag
    ]


def _per_kpi_drift(sid: str, cfg: dict, cells: dict[str, str]) -> list[str]:
    """Each required KPI's OWN resolved budget/acceptable/breaking - the
    numbers `_resolve_required` hands to `verdict_for` - must match the spec
    cell it is scored against, not just the scenario's headline field.

    A plain-string required entry's resolved values ARE the scenario's own
    headline fields (`_resolve_required` copies them verbatim), so checking
    it here would only re-run the scalar check above a second time - worse,
    without that check's ms<->s conversion, so a plain-string entry recorded
    in a different unit family than its cell (S5, S8) would falsely report
    uncheckable. Only DICT-shaped `required` entries carry their OWN numbers
    distinct from the scenario headline, so only those are checked here; the
    case this exists for is S2/S4/S7, each of which requires a SECOND KPI
    carrying its own numbers in the SAME shared cell text as the first, which
    nothing validated before. Numbers are matched by rank within same-family
    DICT entries, in `required` order: the Nth ms-typed dict entry is checked
    against the Nth ms-tagged number the cell states.

    A required threshold with no comparable candidate now FAILS CLOSED (is a
    finding), not silently skipped: an uncheckable value is a value nothing
    can catch drifting, which is worse than a wrong one that at least a human
    reviewing this finding will see. A prose-only cell that is genuinely,
    deliberately unverifiable (S4's provisional target, S7's dropped-keystroke
    thresholds, S2's visual acceptable band sharing the audible-only cell)
    must instead name itself in that entry's `unverifiable_columns` list - an
    explicit, reviewed opt-out beside the entry it applies to, rather than an
    absence of candidates the code cannot tell apart from a stale number
    nobody meant to leave unchecked.

    A DICT entry also carries its OWN `lower_is_better` and `acceptable_strict`,
    each of which can diverge from the scenario headline `threshold_drift`
    already checks once, scenario-wide, against the target cell. Once a
    column's number is confirmed present at the right rank, this also checks
    that number's own leading comparator: for `target`, does it agree with
    this entry's `lower_is_better` (mirroring the scenario-level check, but
    keyed to the entry's own value and its own number's comparator rather
    than the cell's first one); for `acceptable`, does its comparator's
    strictness (no `=` means exclusive) agree with this entry's
    `acceptable_strict`. Both are skipped, like the numeric check above, when
    the column opted out via `unverifiable_columns` or has no comparable
    number at its rank - a column already flagged uncheckable gains nothing
    from a second, redundant finding about its direction.
    """
    problems: list[str] = []
    family_rank: dict[str, int] = {}
    raw_entries = cfg.get("required", [])
    for raw, req in zip(raw_entries, resolve_required(cfg), strict=False):
        if isinstance(raw, str):
            continue
        family = _per_kpi_family(str(req.get("unit", "")))
        rank = family_rank.get(family, 0) + 1
        family_rank[family] = rank
        unverifiable = set(req.get("unverifiable_columns", []))
        for column, value in (
            ("target", req.get("budget")),
            ("acceptable", req.get("acceptable")),
            ("breaking", req.get("breaking")),
        ):
            if value is None:
                continue
            cell = str(cells.get(column, ""))
            candidates = _strict_family_candidates(cell, family)
            if rank > len(candidates):
                if column in unverifiable:
                    continue
                problems.append(
                    f"{sid} {req['kpi']} {column}: map records {value!r}, but spec cell "
                    f"{cell!r} states no comparable number to check it against"
                )
                continue
            if not math.isclose(candidates[rank - 1], float(value), rel_tol=1e-9, abs_tol=1e-9):
                problems.append(
                    f"{sid} {req['kpi']} {column}: map records {value!r}, "
                    f"not found in spec cell {cell!r}"
                )
                continue
            operator = _strict_family_operators(cell, family)[rank - 1]
            problem = _per_kpi_operator_problem(sid, cfg, req, column, operator, cell)
            if problem is not None:
                problems.append(problem)
    return problems


def _per_kpi_operator_problem(
    sid: str, cfg: dict, req: dict, column: str, operator: str | None, cell: str
) -> str | None:
    """One required entry's own direction (`target`) or strictness
    (`acceptable`) against the comparator beside its own confirmed number.

    Split out of `_per_kpi_drift`'s loop to keep that function's own branch
    count under the file's complexity ceiling; the two checks share nothing
    but the "no comparator, nothing to compare" early-out.
    """
    if operator is None:
        return None
    if column == "target":
        lower_is_better = bool(req.get("lower_is_better", cfg.get("lower_is_better", True)))
        cell_is_upper_bound = operator.startswith("<")
        if cell_is_upper_bound != lower_is_better:
            return (
                f"{sid} {req['kpi']} lower_is_better: map records "
                f"{lower_is_better!r}, which disagrees with the comparator "
                f"beside its own number in spec cell {cell!r}"
            )
        return None
    if column == "acceptable":
        acceptable_strict = bool(req.get("acceptable_strict", False))
        cell_is_strict = operator in ("<", ">")
        if cell_is_strict != acceptable_strict:
            return (
                f"{sid} {req['kpi']} acceptable_strict: map records "
                f"{acceptable_strict!r}, which disagrees with the comparator "
                f"beside its own number in spec cell {cell!r}"
            )
    return None


def threshold_drift(kpi_map: dict) -> list[str]:
    """The map's own scoring thresholds must match the spec cell they claim to
    score against, not just its prose (budget_drift's job above).

    budget_drift catches the spec's cell TEXT changing under the map. It
    cannot catch the reverse: a numeric threshold edited (typo, stale copy)
    while the cell text it was derived from stays untouched, which leaves no
    textual signal for budget_drift to see and still ships a verdict against
    a threshold the owning spec no longer states.

    A column whose cell asserts nothing this map's unit can compare against
    is silently left unchecked, not flagged - S4's own target cell ("p95
    frame delta <= display refresh interval") names no number by design, per
    its `missing_kpi` note: the 16.7/33.3ms bands are provisional stand-ins
    for a refresh-relative bound that has no fixed figure to check against
    yet. There is deliberately no separate "acceptable/breaking equal to
    budget" shorthand: that equality is itself no longer trusted as a signal
    that the column has nothing to check, since an accidental edit can
    produce the same equality by coincidence and must still be checked
    against the cell like any other value - "no comparable number in the
    cell" is the only condition that skips a column.

    A scenario's numeric bands mean nothing without knowing which side is
    good: `lower_is_better` flipped with the numbers left untouched passes
    every check above (a "PASS" reading is still <= budget, whichever way
    round it is compared) while silently inverting the verdict. This is
    checked once per scenario against the target cell's own comparator - the
    cell most reliably stating one - rather than per required KPI, since
    direction is a scenario-level scoring choice, not a per-KPI one.

    A scenario-level column with no comparable number in its cell now FAILS
    CLOSED too, mirroring `_per_kpi_drift`'s own opt-out below: an uncheckable
    scalar threshold is a value nothing can catch drifting, exactly the same
    defect as an uncheckable per-KPI one. The scenario's own top-level
    `unverifiable_columns` list is the explicit, reviewed opt-out for a
    genuinely qualitative cell (S1's "any audible glitch during a set", S3's
    "wrong-phase start, or no armed feedback") - distinct from a per-KPI
    entry's own list, which opts out that entry's column, not the scenario's.
    """
    problems: list[str] = []
    for sid, cfg in kpi_map["scenarios"].items():
        cells = cfg.get("spec_cells")
        if not cells:
            continue
        unit = str(cfg.get("unit", ""))
        lower_is_better = bool(cfg.get("lower_is_better", True))
        target_cell = str(cells.get("target", ""))
        if _direction_matches(lower_is_better, "target", target_cell) is False:
            problems.append(
                f"{sid} lower_is_better: map records {lower_is_better!r}, which "
                f"disagrees with the comparator in spec cell {target_cell!r}"
            )
        unverifiable = set(cfg.get("unverifiable_columns", []))
        for column, value in (
            ("target", cfg.get("budget")),
            ("acceptable", cfg.get("acceptable")),
            ("breaking", cfg.get("breaking")),
        ):
            if value is None:
                continue
            cell = str(cells.get(column, ""))
            matches = _threshold_matches_cell(float(value), unit, cell)
            if matches is False:
                problems.append(
                    f"{sid} {column}: map records {value!r}, not found in spec cell {cell!r}"
                )
            elif matches is None and column not in unverifiable:
                problems.append(
                    f"{sid} {column}: map records {value!r}, but spec cell {cell!r} "
                    f"states no comparable number to check it against"
                )
        problems.extend(_per_kpi_drift(sid, cfg, cells))
    return problems
