"""Select and validate one required KPI's reading, independent of scoring it.

Split out of kpi_scorecard.py (Thu 10 Sep 2026) to keep that module under the
600-line file-size ratchet, on the seam the module already had:
picking/trusting a single reading (here) versus combining trusted readings
into a scenario verdict (`score_scenarios`, still in kpi_scorecard.py).

`_classify_required_reading` is the center of this file: it turns one raw
ledger row into either a value worth scoring, a name to report missing, or a
reason to reject it outright, per .claude/rules/verification.md - refuse
rather than assume when a reading cannot be trusted.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from dataclasses import dataclass

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
    capture_id: str | None = None

    @property
    def measured(self) -> bool:
        return self.value is not None and not self.superseded


def newest_reading(entries: list[dict], kpi: str) -> Reading:
    """The newest entry for `kpi`, treating a SUPERSEDED note as not a value.

    A superseded row stays in the ledger on purpose so the retraction is
    visible where the number is, but it must never be scored: that is how a
    withdrawn number keeps being quoted.

    `capture_id`, when a ledger entry names one, identifies the single
    measurement session that produced it. `score_scenarios` uses it to
    refuse combining two required KPIs' readings into one verdict when they
    were recorded in demonstrably different sessions - see its own docstring.
    """
    matches = [e for e in entries if e.get("kpi") == kpi]
    if not matches:
        return Reading(kpi, None, "", None, None, superseded=False)
    # Same-date rows by capture time, not append order (Codex, PR #4474,
    # P2/BLOCKING): hosts publish through one branch, so append order is
    # publish order. Rows without `captured_at` sort first, in append order.
    matches.sort(key=lambda e: (str(e.get("date") or ""), str(e.get("captured_at") or "")))
    newest = matches[-1]
    superseded = SUPERSEDED in str(newest.get("note", ""))
    live = [e for e in matches if SUPERSEDED not in str(e.get("note", ""))]
    if superseded and not live:
        return Reading(
            kpi,
            None,
            str(newest.get("unit", "")),
            newest.get("date"),
            str(newest.get("source") or ""),
            True,
            machine=newest.get("machine"),
            note=newest.get("note"),
            capture_id=newest.get("capture_id"),
        )
    if live:
        newest = live[-1]
    return Reading(
        kpi,
        _as_float(newest.get("value")),
        str(newest.get("unit", "")),
        newest.get("date"),
        str(newest.get("source") or ""),
        superseded=False,
        machine=newest.get("machine"),
        capture_id=newest.get("capture_id"),
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


def age_in_days(date_str: str | None, today: _dt.date) -> int | None:
    """`date_str` comes straight from a JSON ledger row, so its runtime type
    is whatever a contributor typed, not what the `str | None` hint promises;
    a bare int (20260909) or an array survives to here and
    `date.fromisoformat` raises TypeError, not ValueError, for a non-str
    argument. Reject those up front so a malformed row makes one reading
    UNMEASURED instead of crashing the whole scorecard.
    """
    if not isinstance(date_str, str) or not date_str:
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


@dataclass(frozen=True)
class _ReadingCheck:
    """Exactly one of these three is set, per how `_classify_required_reading`
    resolved one required KPI's reading. A dataclass with three plain-typed
    slots, rather than a `(status, str | tuple[float, int])` pair, so the
    caller narrows by an `is not None` check mypy can actually follow.

    `ok`'s first element is the req to score against, not necessarily the one
    passed in: `_resolve_cache_state_band` can swap in a state-specific band.
    """

    ok: tuple[dict, float, int] | None = None
    missing_name: str | None = None
    rejected_reason: str | None = None


def _resolve_cache_state_band(req: dict, note: str | None) -> dict | None:
    """`req` with `budget`/`acceptable` swapped to the cache state named in
    `note`, or `req` unchanged when it names no per-state bands.

    A KPI whose spec gives a warm population and a cold population different
    thresholds (S5: <=2s warm target, <=5s cold acceptable) cannot share one
    budget/acceptable pair without silently picking a side: the generous cold
    band would let a warm load that missed its 2s target by nearly 4x still
    read ACCEPTABLE. `req["cache_state_bands"]` names each state's own
    budget/acceptable; the reading's own note must name exactly one state, or
    this returns None - guessing which band applies is the same
    invented-threshold defect this closes, one level down.
    """
    bands = req.get("cache_state_bands")
    if not bands:
        return req
    haystack = (note or "").lower()
    matched = [state for state in bands if state in haystack]
    if len(matched) != 1:
        return None
    band = bands[matched[0]]
    return {**req, "budget": band["budget"], "acceptable": band["acceptable"]}


def _classify_required_reading(
    req: dict, by_name: dict[str, Reading], today: _dt.date
) -> _ReadingCheck:
    """One required KPI's reading: present and trustworthy (`ok`), absent
    (`missing_name`), or present but not trustworthy (`rejected_reason`).

    Split out of `score_scenarios`'s own loop to keep that function's branch
    count under the file's complexity ceiling; the four rejection checks
    share nothing but "this reading cannot be trusted, stop here".
    """
    name = req["kpi"]
    reading = by_name.get(name)
    if reading is None or not reading.measured or reading.value is None:
        return _ReadingCheck(missing_name=name)
    if reading.unit != req["unit"]:
        return _ReadingCheck(
            rejected_reason=f"{name} recorded in {reading.unit!r}, map expects {req['unit']!r}"
        )
    age = age_in_days(reading.date, today)
    if age is None or age < 0:
        return _ReadingCheck(
            rejected_reason=(
                f"{name} has a missing, malformed, or future-dated reading ({reading.date!r})"
            )
        )
    if _needs_provenance(name, req["unit"]) and not (reading.machine and reading.note):
        return _ReadingCheck(
            rejected_reason=(
                f"{name} is a p95/percentage KPI recorded without a machine tier "
                f"and measurement window/denominator (machine={reading.machine!r}, "
                f"note={reading.note!r}), required by the denominator-honesty note "
                "in specs/perf-latency-program.md"
            )
        )
    if not reading.source:
        return _ReadingCheck(
            rejected_reason=(
                f"{name} has no recorded source, so its measurement cannot be "
                "traced back to evidence"
            )
        )
    effective_req = _resolve_cache_state_band(req, reading.note)
    if effective_req is None:
        states = sorted(req["cache_state_bands"])
        return _ReadingCheck(
            rejected_reason=(
                f"{name}'s threshold depends on cache state ({', '.join(states)}), which "
                f"its note ({reading.note!r}) does not name exactly one of; scoring it "
                "against either band without knowing which applies would overstate or "
                "understate the reading"
            )
        )
    return _ReadingCheck(ok=(effective_req, reading.value, age))


def _normalize_capture_id(raw: object) -> str | None:
    """A ledger row's `capture_id` reduced to `None` (absent, blank, or not a
    string) or the stripped string itself. Closes two reviewer-found gaps: a
    blank string used to count as one NAMED cohort (the set held one member,
    not `None`); a non-string JSON value (an array or object) reached a
    bare `{...}` set literal and crashed with `TypeError: unhashable type`.
    """
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def _cohort_mismatch_reason(
    scoreable: list[tuple[dict, float, int]], by_name: dict[str, Reading]
) -> str | None:
    """Do the scoreable required readings fail to share one NAMED evidence
    cohort (capture_id), so they must not be combined into one verdict?

    Split out of `score_scenarios` to keep that function's branch count
    under the file's complexity ceiling; see that function's own docstring
    for why an unnamed capture id does not excuse a reading from this check.
    """
    capture_ids = {
        _normalize_capture_id(by_name[req["kpi"]].capture_id) for req, _, _ in scoreable
    }
    if len(capture_ids) == 1 and None not in capture_ids:
        return None
    return "required KPIs do not share one evidence cohort (capture_id): " + ", ".join(
        f"{req['kpi']}={by_name[req['kpi']].capture_id!r}" for req, _, _ in scoreable
    )


def _missing_rejected_reasons(missing: list[str], rejected: list[str]) -> list[str]:
    """The shared "required KPI not recorded"/"required KPI rejected" prose,
    built once instead of twice in `score_scenarios`'s two note-assembly
    branches, to keep that function's branch count under the complexity
    ceiling.
    """
    reasons = []
    if missing:
        reasons.append(f"required KPI not recorded: {', '.join(missing)}")
    if rejected:
        reasons.append(f"required KPI rejected: {'; '.join(rejected)}")
    return reasons
