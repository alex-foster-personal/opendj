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
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.perf.kpi_map_drift import (
    budget_drift,
    drift,
    resolve_required,
    spec_scenario_ids,
    threshold_drift,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = _REPO_ROOT / "specs" / "perf-latency-program.md"
_MAP = _REPO_ROOT / "docs" / "perf" / "kpi-map.json"
_LEDGER = _REPO_ROOT / "docs" / "perf" / "kpi-ledger.json"

UNMEASURED = "UNMEASURED"
SUPERSEDED = "SUPERSEDED"

_PERCENTILE = re.compile(r"p\d{2,3}")


@dataclass(frozen=True)
class Reading:
    """One KPI's newest surviving measurement, or the absence of one."""

    kpi: str
    value: float | None
    unit: str
    date: str | None
    source: str | None
    superseded: bool
    machine: str | None = None
    note: str | None = None

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
            machine=newest.get("machine"),
            note=newest.get("note"),
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
        machine=newest.get("machine"),
        note=newest.get("note"),
    )


def _as_float(value: object) -> float | None:
    """A probed value, or None when it is not a trustworthy measurement.

    Per docs/perf/learnings/_INDEX.md L-line convention, a probe returns -1
    (never 0) when its subject never appeared, so a legitimate zero-valued
    measurement (e.g. zero dropped keystrokes) must stay distinct from an
    absent one. Rejecting any negative number therefore catches the sentinel
    without touching a real reading, since every KPI this scores is a count,
    duration, or rate that cannot go negative. NaN and +/-inf are rejected the
    same way: neither is a value verdict_for can compare against a threshold.
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0:
        return None
    return result


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


def _needs_provenance(name: str, unit: str) -> bool:
    """Is `name` a percentile, percentage, or rate KPI, which the spec's own
    denominator-honesty note (specs/perf-latency-program.md, 'Denominator
    honesty') requires to name its window and machine tier?

    Scoped to percentile/percentage/rate KPIs specifically, matching the
    note's own examples (dropped frames, xruns/hour, p95s) rather than every
    KPI: a plain duration or count (deck load seconds, dropped-keystroke
    count) has no window or sample size to misrepresent the same way a
    percentile, a rate, or a percentage does. "hour" catches S1's xruns/hour
    and silent-while-playing rate KPIs, the note's own rate example, without
    matching a plain duration. The percentile match is a pattern
    (`_PERCENTILE`, mirroring kpi_map_drift.py's `_PERCENTILE_LABEL`) rather
    than a literal "p95", so renaming a KPI to p99 or any other percentile
    cannot silently walk it out from under this gate.
    """
    haystack = f"{name} {unit}".lower()
    return (
        bool(_PERCENTILE.search(haystack))
        or "%" in haystack
        or "pct" in haystack
        or "hour" in haystack
    )


def score_scenarios(kpi_map: dict, entries: list[dict], today: _dt.date) -> list[Score]:
    """Score each scenario on its REQUIRED KPIs alone.

    Anything else bound to a scenario is context: it prints, it never scores.
    A reviewer showed why an any-measured test was not enough. S2 would have
    reported PASS the moment press-to-schedule was recorded, while
    press-to-AUDIBLE, the number the scenario exists for, was still missing.
    That recreates the partial pass this tool exists to prevent.

    A required reading that cannot be trusted scores nothing, per
    .claude/rules/verification.md: refuse rather than assume. Two ways a
    reading fails that trust without ever being absent: it is recorded in a
    unit the map does not expect, or its date is missing, malformed, or in the
    future. Either one used to fall through to verdict_for anyway - a wrong
    unit produced a false verdict outright, and a bad date produced a real
    verdict with no age, which let `--max-stale-days` exit 0 on evidence it
    never actually checked.

    A confirmed BREAKING verdict among the scoreable required KPIs is
    reported as BREAKING even when another required KPI is still missing or
    rejected, rather than collapsing to UNMEASURED. Missing evidence
    elsewhere cannot un-break a KPI that has already, conclusively, broken:
    S7's dropped-keystroke count reading BREAKING must not vanish just
    because the paired latency p95 has not been recorded yet. This does not
    extend to OVER or ACCEPTABLE, which are directional judgments a still-
    missing companion KPI could plausibly change the overall picture around;
    only BREAKING is unconditionally the floor.
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
            name = req["kpi"]
            reading = by_name.get(name)
            if reading is None or not reading.measured or reading.value is None:
                missing.append(name)
                continue
            if reading.unit != req["unit"]:
                rejected.append(
                    f"{name} recorded in {reading.unit!r}, map expects {req['unit']!r}"
                )
                continue
            age = age_in_days(reading.date, today)
            if age is None or age < 0:
                rejected.append(
                    f"{name} has a missing, malformed, or future-dated reading "
                    f"({reading.date!r})"
                )
                continue
            if _needs_provenance(name, req["unit"]) and not (reading.machine and reading.note):
                rejected.append(
                    f"{name} is a p95/percentage KPI recorded without a machine tier "
                    f"and measurement window/denominator (machine={reading.machine!r}, "
                    f"note={reading.note!r}), required by the denominator-honesty note "
                    "in specs/perf-latency-program.md"
                )
                continue
            if not reading.source:
                rejected.append(
                    f"{name} has no recorded source, so its measurement cannot be "
                    "traced back to evidence"
                )
                continue
            scoreable.append((req, reading.value, age))

        verdicts = [verdict_for(value, req) for req, value, _ in scoreable]
        conclusive_breaking = "BREAKING" in verdicts

        if not required or (not conclusive_breaking and (missing or rejected)):
            note = str(cfg.get("missing_kpi", "")) or "no KPI is bound to this scenario"
            reasons = []
            if missing:
                reasons.append(f"required KPI not recorded: {', '.join(missing)}")
            if rejected:
                reasons.append(f"required KPI rejected: {'; '.join(rejected)}")
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
            reasons = []
            if missing:
                reasons.append(f"required KPI not recorded: {', '.join(missing)}")
            if rejected:
                reasons.append(f"required KPI rejected: {'; '.join(rejected)}")
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
                lines.append(
                    f"       {r.kpi} = {r.value} {r.unit}  "
                    f"({r.date}, {r.machine}, {r.source})"
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

    threshold_problems = threshold_drift(kpi_map)
    if threshold_problems:
        print("[kpi-scorecard] THRESHOLD DRIFT, refusing to score:", file=sys.stderr)
        for problem in threshold_problems:
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
