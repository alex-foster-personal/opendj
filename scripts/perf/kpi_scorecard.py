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
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.perf.kpi_map_drift import (
    budget_drift,
    class_drift,
    drift,
    resolve_required,
    spec_scenario_ids,
    threshold_drift,
)
from scripts.perf.kpi_readings import (
    Reading,
    _classify_required_reading,
    _cohort_mismatch_reason,
    _missing_rejected_reasons,
    newest_reading,
)
from scripts.perf.kpi_readings import age_in_days as age_in_days  # re-exported for callers

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = _REPO_ROOT / "specs" / "perf-latency-program.md"
_MAP = _REPO_ROOT / "docs" / "perf" / "kpi-map.json"
_LEDGER = _REPO_ROOT / "docs" / "perf" / "kpi-ledger.json"

UNMEASURED = "UNMEASURED"

# Reading selection/validation (Reading, newest_reading, age_in_days, the
# _classify_required_reading family) lives in kpi_readings.py; imported above
# and re-exported here so `from scripts.perf.kpi_scorecard import Reading`
# etc. keeps working for existing callers and tests.


@dataclass(frozen=True)
class Score:
    scenario: str
    title: str
    ux_class: str
    verdict: str
    readings: tuple[Reading, ...]
    age_days: int | None
    note: str


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

    `acceptable_strict`, when true, makes the acceptable boundary exclusive
    (`<`/`>`) rather than the default inclusive (`<=`/`>=`). Most spec cells
    state their acceptable bound as "<=", which the default matches; S4's
    dropped-frame cell states "<5%" with no equals, so a reading of exactly
    5% must report OVER, not ACCEPTABLE. The map still records the value the
    spec cell literally states (5), matched by threshold_drift as normal;
    only the comparison operator changes.
    """
    lower_is_better = bool(cfg.get("lower_is_better", True))
    budget = float(cfg["budget"])
    acceptable = float(cfg["acceptable"])
    acceptable_strict = bool(cfg.get("acceptable_strict", False))
    raw_breaking = cfg.get("breaking")
    breaking = None if raw_breaking is None else float(raw_breaking)
    if lower_is_better:
        if value <= budget:
            return "PASS"
        within_acceptable = value < acceptable if acceptable_strict else value <= acceptable
        if within_acceptable:
            return "ACCEPTABLE"
        if breaking is not None and value > breaking:
            return "BREAKING"
        return "OVER"
    if value >= budget:
        return "PASS"
    within_acceptable = value > acceptable if acceptable_strict else value >= acceptable
    if within_acceptable:
        return "ACCEPTABLE"
    if breaking is not None and value < breaking:
        return "BREAKING"
    return "OVER"


def score_scenarios(kpi_map: dict, entries: list[dict], today: _dt.date) -> list[Score]:
    """Score each scenario on its REQUIRED KPIs alone.

    Anything else bound to a scenario is context: it prints, it never scores.
    An any-measured test was not enough - S2 would report PASS the moment
    press-to-schedule was recorded, while press-to-AUDIBLE, the number the
    scenario exists for, was still missing. That is the partial pass this
    tool exists to prevent.

    A required reading that cannot be trusted scores nothing, per
    .claude/rules/verification.md: refuse rather than assume. Two ways a
    reading fails that trust without being absent: a unit the map does not
    expect, or a date missing, malformed, or in the future. Either used to
    fall through to verdict_for anyway - a wrong unit produced a false
    verdict outright, a bad date a real verdict with no age, letting
    `--max-stale-days` exit 0 on evidence it never checked.

    A confirmed BREAKING verdict among the scoreable required KPIs reports
    BREAKING even when another required KPI is still missing or rejected,
    rather than collapsing to UNMEASURED: missing evidence elsewhere cannot
    un-break a KPI that has already, conclusively, broken (S7's
    dropped-keystroke count BREAKING must not vanish for want of a paired
    latency p95). OVER/ACCEPTABLE do not get this floor, since a still-
    missing companion could plausibly change the picture; only BREAKING does.

    A scenario requiring more than one KPI (S2, S4, S6, S7) must not combine
    them into one non-BREAKING verdict unless the evidence says they were
    captured together: each required KPI's newest reading is picked
    independently, so nothing stops an audible p99 from one session pairing
    with a visual p95 from an unrelated one. A ledger entry's `capture_id`
    names its session; readings combine ONLY when every one names the SAME
    non-null, non-blank id - missing, blank, or differing all reject the same
    way, none can prove a shared session. As above, a conclusively BREAKING
    KPI needs no cohort at all.
    """
    scores: list[Score] = []
    for sid, cfg in kpi_map["scenarios"].items():
        required = resolve_required(cfg)
        readings = tuple(newest_reading(entries, name) for name in cfg.get("kpis", []))
        by_name = {r.kpi: r for r in readings}

        missing: list[str] = []
        rejected: list[str] = []
        scoreable: list[tuple[dict, float, int]] = []
        for req in required:
            check = _classify_required_reading(req, by_name, today)
            if check.ok is not None:
                effective_req, value, age = check.ok
                scoreable.append((effective_req, value, age))
            elif check.missing_name is not None:
                missing.append(check.missing_name)
            elif check.rejected_reason is not None:
                rejected.append(check.rejected_reason)

        verdicts = [verdict_for(value, req) for req, value, _ in scoreable]
        conclusive_breaking = "BREAKING" in verdicts

        if not conclusive_breaking and len(scoreable) > 1:
            cohort_problem = _cohort_mismatch_reason(scoreable, by_name)
            if cohort_problem is not None:
                rejected.append(cohort_problem)
                scoreable = []
                verdicts = []

        if not required or (not conclusive_breaking and (missing or rejected)):
            note = str(cfg.get("missing_kpi", "")) or "no KPI is bound to this scenario"
            reasons = _missing_rejected_reasons(missing, rejected)
            if reasons:
                note = ". ".join(reasons) + ". " + note
            scores.append(
                Score(sid, cfg["title"], cfg.get("class", "?"), UNMEASURED, readings, None, note)
            )
            continue

        worst = next(
            (level for level in ("BREAKING", "OVER", "ACCEPTABLE") if level in verdicts),
            "PASS",
        )
        ages = [age for _, _, age in scoreable]
        if missing or rejected:
            reasons = _missing_rejected_reasons(missing, rejected)
            note = "BREAKING confirmed despite incomplete evidence elsewhere: " + "; ".join(
                reasons
            )
        else:
            note = str(cfg.get("caveat", ""))
        scores.append(
            Score(
                sid,
                cfg["title"],
                cfg.get("class", "?"),
                worst,
                readings,
                max(ages) if ages else None,
                note,
            )
        )
    return scores


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
                cohort = f", capture={r.capture_id}" if r.capture_id else ""
                lines.append(
                    f"       {r.kpi} = {r.value} {r.unit}  "
                    f"({r.date}, {r.machine}, {r.source}{cohort})"
                )
                if r.note:
                    lines.append(f"         note: {r.note}")
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


def _score_to_json(s: Score) -> dict:
    """One `Score`'s `--json` shape, including the per-reading provenance
    (source, machine, note, capture_id) the text renderer also prints -
    split out so a test can assert on this shape directly rather than only
    through a subprocess against the live ledger.
    """
    return {
        "scenario": s.scenario,
        "class": s.ux_class,
        "verdict": s.verdict,
        "age_days": s.age_days,
        "title": s.title,
        "note": s.note,
        "readings": [
            {
                "kpi": r.kpi,
                "value": r.value,
                "unit": r.unit,
                "date": r.date,
                "source": r.source,
                "machine": r.machine,
                "note": r.note,
                "superseded": r.superseded,
                "capture_id": r.capture_id,
            }
            for r in s.readings
        ],
    }


def _report_drift(label: str, problems: list[str]) -> None:
    """Print one drift check's findings in the shared refuse-to-score shape.

    Split out of `main` so BUDGET/CLASS/THRESHOLD drift share one branch in
    the caller instead of three near-identical `if` blocks, keeping `main`
    under the file's complexity ceiling.
    """
    print(f"[kpi-scorecard] {label} DRIFT, refusing to score:", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)


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

    for label, problems in (
        ("BUDGET", budget_drift(kpi_map, spec_text)),
        ("CLASS", class_drift(kpi_map, spec_text)),
        ("THRESHOLD", threshold_drift(kpi_map)),
    ):
        if problems:
            _report_drift(label, problems)
            return 1

    scores = score_scenarios(kpi_map, ledger["entries"], today)
    if args.json:
        print(json.dumps([_score_to_json(s) for s in scores], indent=2))
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
