"""Score the perf program's UX scenarios against what has actually been measured.

The problem this exists for: the KPI ledger is an append-only pile of numbers
from different rounds, and the budgets live in a separate spec table. Nothing
joined them, so on Wed 9 Sep 2026 four of the user-felt KPIs had been stale
since Mon 31 Aug 2026 and nobody noticed, while two others had been silently
refuted by a tool that hardened in review. Staleness and absence were both
invisible.

Two rules shape the output, both from .claude/rules/verification.md:

- A scenario nobody can score reads UNMEASURED, never PASS. A board with zero
  failing checks because it has zero checks is the defect this tool is built to
  make impossible.
- A stale value is reported with its age in days, because a recorded verdict is
  a measurement with its timestamp removed.
- A KPI that measures something ADJACENT to its scenario scores nothing. The
  first run of this tool handed three scenarios a PASS off proxies whose own
  notes said they measured a different thing: a server-side walk time standing
  in for a pane's time to interactive, a settle time standing in for a p95, and
  an idle instrument check standing in for a glitch rate during a real set.
  `proxy_only` in the map forces those to UNMEASURED, because the flattering
  reading is the one that happens by default.

Exit codes are deliberately narrow. Drift between the map and the spec exits 1,
because that is a defect in the instrument itself and is always actionable. A
missed budget or a stale reading exits 0: those are the program's work, and a
red exit on them would be ignored within a week. `--max-stale-days` opts into
failing on staleness for a caller that wants to gate on it.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = _REPO_ROOT / "specs" / "perf-latency-program.md"
_MAP = _REPO_ROOT / "docs" / "perf" / "kpi-map.json"
_LEDGER = _REPO_ROOT / "docs" / "perf" / "kpi-ledger.json"

_SCENARIO_ROW = re.compile(r"^\|\s*(S\d+)\s*\|", re.MULTILINE)

UNMEASURED = "UNMEASURED"
SUPERSEDED = "SUPERSEDED"


@dataclass(frozen=True)
class Reading:
    """One KPI's newest surviving measurement, or the absence of one."""

    kpi: str
    value: float | None
    unit: str
    date: str | None
    source: str | None
    superseded: bool

    @property
    def measured(self) -> bool:
        return self.value is not None and not self.superseded


@dataclass(frozen=True)
class Score:
    scenario: str
    title: str
    ux_class: str
    verdict: str
    readings: tuple[Reading, ...]
    age_days: int | None
    note: str


# ---------------------------------------------------------------- loading


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


def newest_reading(entries: list[dict], kpi: str) -> Reading:
    """The newest entry for `kpi`, treating a SUPERSEDED note as not a value.

    A superseded row stays in the ledger on purpose so the retraction is
    visible where the number is, but it must never be scored: that is how a
    withdrawn number keeps being quoted.
    """
    matches = [e for e in entries if e.get("kpi") == kpi]
    if not matches:
        return Reading(kpi, None, "", None, None, superseded=False)
    matches.sort(key=lambda e: e.get("date", ""))
    newest = matches[-1]
    superseded = SUPERSEDED in str(newest.get("note", ""))
    live = [e for e in matches if SUPERSEDED not in str(e.get("note", ""))]
    if superseded and not live:
        return Reading(
            kpi,
            None,
            str(newest.get("unit", "")),
            newest.get("date"),
            str(newest.get("source", "")),
            True,
        )
    if live:
        newest = live[-1]
    return Reading(
        kpi,
        _as_float(newest.get("value")),
        str(newest.get("unit", "")),
        newest.get("date"),
        str(newest.get("source", "")),
        superseded=False,
    )


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


# ---------------------------------------------------------------- scoring


def verdict_for(value: float, cfg: dict) -> str:
    """PASS at target, ACCEPTABLE inside the looser band, OVER past that, BREAKING past `breaking`.

    The OVER band exists because the spec declares THREE thresholds and an
    earlier version of this function read only two, calling everything past
    `acceptable` BREAKING. That made the tool's only measured headline verdict
    false: an 8,038 ms reading was reported as breaking against a spec that
    defines breaking for that scenario as greater than 10,000 ms.

    `breaking` may be None, meaning the spec declines to define one. Such a
    scenario can report OVER but never BREAKING, rather than having a threshold
    invented for it here.
    """
    lower_is_better = bool(cfg.get("lower_is_better", True))
    budget = float(cfg["budget"])
    acceptable = float(cfg["acceptable"])
    raw_breaking = cfg.get("breaking")
    breaking = None if raw_breaking is None else float(raw_breaking)
    if lower_is_better:
        if value <= budget:
            return "PASS"
        if value <= acceptable:
            return "ACCEPTABLE"
        if breaking is not None and value > breaking:
            return "BREAKING"
        return "OVER"
    if value >= budget:
        return "PASS"
    if value >= acceptable:
        return "ACCEPTABLE"
    if breaking is not None and value < breaking:
        return "BREAKING"
    return "OVER"


def age_in_days(date_str: str | None, today: _dt.date) -> int | None:
    if not date_str:
        return None
    try:
        measured = _dt.date.fromisoformat(date_str)
    except ValueError:
        return None
    return (today - measured).days


def score_scenarios(kpi_map: dict, entries: list[dict], today: _dt.date) -> list[Score]:
    """Score each scenario on its REQUIRED KPIs alone.

    Anything else bound to a scenario is context: it prints, it never scores.
    A reviewer showed why an any-measured test was not enough. S2 would have
    reported PASS the moment press-to-schedule was recorded, while
    press-to-AUDIBLE, the number the scenario exists for, was still missing.
    That recreates the partial pass this tool exists to prevent.
    """
    scores: list[Score] = []
    for sid, cfg in kpi_map["scenarios"].items():
        required = list(cfg.get("required", []))
        readings = tuple(newest_reading(entries, name) for name in cfg.get("kpis", []))
        by_name = {r.kpi: r for r in readings}
        required_readings = [by_name[name] for name in required if name in by_name]
        missing = [name for name in required if name not in by_name or not by_name[name].measured]
        if not required or missing:
            note = str(cfg.get("missing_kpi", "")) or "no KPI is bound to this scenario"
            if missing:
                note = f"required KPI not recorded: {', '.join(missing)}. " + note
            scores.append(
                Score(sid, cfg["title"], cfg.get("class", "?"), UNMEASURED, readings, None, note)
            )
            continue
        verdicts = [verdict_for(r.value, cfg) for r in required_readings if r.value is not None]
        worst = next(
            (level for level in ("BREAKING", "OVER", "ACCEPTABLE") if level in verdicts),
            "PASS",
        )
        ages = [a for a in (age_in_days(r.date, today) for r in required_readings) if a is not None]
        scores.append(
            Score(
                sid,
                cfg["title"],
                cfg.get("class", "?"),
                worst,
                readings,
                max(ages) if ages else None,
                str(cfg.get("caveat", "")),
            )
        )
    return scores


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


# ---------------------------------------------------------------- output


def render(scores: list[Score], max_stale_days: int | None) -> list[str]:
    lines = ["", f"{'':4} {'class':5} {'verdict':11} {'age':>5}  scenario", "-" * 96]
    for s in scores:
        age = "-" if s.age_days is None else f"{s.age_days}d"
        stale = ""
        if max_stale_days is not None and s.age_days is not None and s.age_days > max_stale_days:
            stale = "  STALE"
        lines.append(f"{s.scenario:4} {s.ux_class:5} {s.verdict:11} {age:>5}  {s.title}{stale}")
        for r in s.readings:
            if r.measured:
                lines.append(f"       {r.kpi} = {r.value} {r.unit}  ({r.date}, {r.source})")
            elif r.superseded:
                lines.append(f"       {r.kpi} = SUPERSEDED, not scored")
            else:
                lines.append(f"       {r.kpi} = never recorded")
        if s.note:
            label = "WHY UNMEASURED" if s.verdict == UNMEASURED else "READ THIS WITH THE VERDICT"
            lines.append(f"       {label}: {s.note}")
    counts: dict[str, int] = {}
    for s in scores:
        counts[s.verdict] = counts.get(s.verdict, 0) + 1
    lines.append("")
    lines.append("  " + ", ".join(f"{n} {v}" for v, n in sorted(counts.items())))
    lines.append(
        f"  {counts.get(UNMEASURED, 0)} of {len(scores)} UX scenarios cannot be scored at all. "
        "An UNMEASURED scenario is not a passing one."
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score UX scenarios against measured KPIs.")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--max-stale-days",
        type=int,
        default=None,
        help="exit 1 when a scored KPI is older than this",
    )
    parser.add_argument("--today", default=None, help="ISO date to age against (testing)")
    args = parser.parse_args(argv)

    kpi_map = json.loads(_MAP.read_text())
    ledger = json.loads(_LEDGER.read_text())
    spec_ids = spec_scenario_ids(_SPEC.read_text())
    today = _dt.date.fromisoformat(args.today) if args.today else _dt.datetime.now(_dt.UTC).date()

    spec_text = _SPEC.read_text()
    unmapped, unknown = drift(kpi_map, spec_ids)
    if unmapped or unknown:
        print("[kpi-scorecard] MAP DRIFT, refusing to score:", file=sys.stderr)
        for sid in unmapped:
            print(f"  spec declares {sid}; the map does not score it", file=sys.stderr)
        for sid in unknown:
            print(f"  the map scores {sid}; the spec no longer declares it", file=sys.stderr)
        return 1

    budget_problems = budget_drift(kpi_map, spec_text)
    if budget_problems:
        print("[kpi-scorecard] BUDGET DRIFT, refusing to score:", file=sys.stderr)
        for problem in budget_problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    scores = score_scenarios(kpi_map, ledger["entries"], today)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "scenario": s.scenario,
                        "class": s.ux_class,
                        "verdict": s.verdict,
                        "age_days": s.age_days,
                        "title": s.title,
                        "readings": [
                            {
                                "kpi": r.kpi,
                                "value": r.value,
                                "unit": r.unit,
                                "date": r.date,
                                "superseded": r.superseded,
                            }
                            for r in s.readings
                        ],
                    }
                    for s in scores
                ],
                indent=2,
            )
        )
    else:
        print("\n".join(render(scores, args.max_stale_days)))

    if args.max_stale_days is not None:
        stale = [s for s in scores if s.age_days is not None and s.age_days > args.max_stale_days]
        if stale:
            print(
                f"[kpi-scorecard] {len(stale)} scored scenario(s) older "
                f"than {args.max_stale_days}d",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
