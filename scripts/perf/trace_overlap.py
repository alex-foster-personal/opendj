"""Do the frontend ring and the native probe log actually describe one session?

Split out of export_chrome_trace.py when that module crossed the 600-line
ceiling. The seam is the subject: export_chrome_trace.py owns the MERGE (one
origin, names, the trace object), while this module owns the HONESTY CHECK --
whether the two layers overlap in time at all, and what each one's counters
actually cover.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

# How far apart two probe gaps may be and still count as the SAME cadence.
# Sized against the two things it has to separate, not tuned to a fixture:
# real jitter on the shipped 15s probe is sub-percent (the one real capture
# in tests/fixtures/perf/ is 0.8% off nominal), while the outages this
# function must never adopt are minutes to hours, i.e. whole multiples. 1.25
# sits with orders of magnitude of clearance on both sides, so neither
# separation depends on the exact value. It is a RATIO rather than an
# absolute microsecond window on purpose: a probe run at a different
# --interval jitters proportionally, and a fixed window would silently mean
# something different at 1s than at 60s.
CADENCE_JITTER_RATIO = 1.25

# ------------------------------------------------------------------ spans


def _window_us(events: list[dict[str, Any]]) -> tuple[int, int] | None:
    """The wall-clock span a layer covers. `dur` is part of the end, not
    decoration: a load's last PHASE starts before it finishes, so a
    ts-only maximum understates the true end."""

    starts = [event["ts"] for event in events if "ts" in event]
    if not starts:
        return None
    ends = [event["ts"] + event.get("dur", 0) for event in events if "ts" in event]
    return (min(starts), max(ends))


def _merged_spans_us(events: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """Every interval a layer ACTUALLY occupies, merged and in order.

    The envelope above answers "when did this layer start and stop"; this
    answers "when was it doing anything", and only the second compares
    honestly: a ring can hold Monday's and Wednesday's loads while the probe
    ran only Tuesday, and their envelopes contain each other. Codex found it
    on #705.
    """

    spans = sorted(
        (event["ts"], event["ts"] + event.get("dur", 0))
        for event in events
        if "ts" in event
    )
    if not spans:
        return []
    merged: list[list[int]] = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _sampled_spans_us(events: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """The native layer's spans, with each sample widened to what it MEASURES.

    A probe counter is a DELTA since the previous sample, so a record stamped
    t reports on the window that ended at t, not an instant. Encoded as the
    zero-wide event it is serialized as, samples at t=0 and t=15 bracketing a
    load at t=5..10 read as 5s from either sample and print a false
    different-session warning. Codex found it on #705.

    Each sample widens BACKWARD by the log's own agreeing cadence
    (`_agreeing_cadence`), not a nominal 15s. Widening backward instead of
    bridging pairs keeps a probe outage honest: a log stopped Monday and
    restarted Wednesday extends by one cadence at the seam, not across both
    days (bridging every pair would erase it -- the mistake `_merged_spans_us`
    exists to avoid, from the other side). A voted-on scalar (median, or any
    tightest-cluster variant) can always be dragged onto an outage's own
    width once enough outages accumulate in one run -- no gap-value statistic
    can tell an outage-dominated majority apart from a real one, since both
    are just a majority-sized run of identical values (Codex P1/BLOCKING,
    #705, discussion_r3921230927 and discussion_r3921398656).
    `_agreeing_cadence` instead takes the SMALLEST gap that repeats: an
    outage is a gap much LONGER than normal sampling by definition, so the
    smallest recurring width can never be an outage's, only an interval the
    probe actually achieved more than once -- the "repeats" half also
    stops a single overrun sample from collapsing the whole run's cadence to
    its own one-off width (Codex P2/NON-BLOCKING, #705,
    discussion_r3921525197). Under four stamps, or with no gap repeated at
    all: UNKNOWN (0), not a guess.

    Three more edge cases Codex found here, each fixed in its own round:
    only COUNTER events (`ph == "C"`) read for cadence, since a probe error
    is a zero-width instant that covers no interval and would drag the
    median without measuring anything; neither a FILE's nor a RUN's first
    sample ever widens, since `OpenDJProbe.prior_cpu` resets on every
    process start so `cpu_percent` is unmeasured there too -- but
    `footprint_mb` is a direct read and still gets sampled, so a stamp with
    a footprint sample and no cpu sample IS that first-of-run record; and
    cadence is derived PER RUN, not once for the file, since a file-global
    median lets a prior run's interval outvote a resumed run's own, so a
    partition opens at each run-start timestamp (the same ones exempted
    above) and every stamp's cadence comes only from its own partition.
    """

    samples = [event for event in events if event.get("ph") == "C" and "ts" in event]
    stamps = sorted({event["ts"] for event in samples})
    earliest = stamps[0] if stamps else None
    cpu_measured_stamps = {
        event["ts"] for event in samples if event.get("name") == "cpu_percent"
    }
    run_starts = sorted(
        ({earliest} if earliest is not None else set())
        | {ts for ts in stamps if ts not in cpu_measured_stamps}
    )
    cadence_by_ts = _cadence_by_run(stamps, run_starts)
    # A run's own median can still overshoot its FIRST interval: a deep probe
    # iteration that overran `--interval` fires the second sample almost
    # immediately, and the other, regular gaps outvote that one short gap in
    # the median. Widening backward by the full median then reaches before
    # the run's own preceding sample -- for the second sample, before the run
    # ever started -- and claims coverage of a load the probe could not have
    # observed. Cap each widen at the ACTUAL gap to the preceding stamp, so
    # the backward reach can never pass the run boundary (Codex P1/BLOCKING,
    # #705, export_chrome_trace.py:217).
    gap_before_ts = {later: later - earlier for earlier, later in pairwise(stamps)}
    widened = [
        event
        if event["ts"] == earliest or event["ts"] not in cpu_measured_stamps
        else {
            **event,
            "ts": event["ts"] - min(cadence_by_ts[event["ts"]], gap_before_ts[event["ts"]]),
            "dur": event.get("dur", 0)
            + min(cadence_by_ts[event["ts"]], gap_before_ts[event["ts"]]),
        }
        for event in samples
    ]
    instants = [
        event for event in events if event.get("ph") != "C" and "ts" in event
    ]
    return _merged_spans_us(widened + instants)


def _agreeing_cadence(gaps: list[int]) -> int:
    """The shortest interval this run actually demonstrated.

    A plain median only resists ONE outlier's worth of shift: with an
    EVEN interval count, `statistics.median` averages its two middle
    values, so two outages among otherwise-regular intervals can land both
    middle-sorted values on the outage side and blend them into a cadence
    that is itself an outage's width (repeated suspend/resume, or repeated
    probe failures, produce exactly this shape). Requiring four stamps only
    protects against one outlier, not multiple outages (Codex P1/BLOCKING,
    #705, discussion_r3921230927).

    Two rounds tried to vote a scalar cadence out of the gaps -- tightest
    majority window, then internally-tight majority window -- and both lost
    to the same shape: three identical 1h outages among five gaps are a
    zero-spread, majority-sized cluster no tightness test can ever tell
    apart from three identical 15s samples (Codex P1/BLOCKING, #705,
    discussion_r3921398656). No statistic over gap VALUES can resolve that;
    it needs the direction outages and cadence differ in, which the gaps
    alone don't carry.

    Fix, round one: take the minimum. That is never an outage's width (an
    outage is defined as much LONGER than normal sampling), but a plain
    minimum trusts ANY gap once, including a single iteration that overran
    `--interval` and fired the next sample almost immediately -- one 0.1s
    overrun among two 15s intervals then collapses the WHOLE run's cadence
    to 0.1s, under-widening every other, genuinely-15s-spaced sample and
    reintroducing this file's original false different-session warning
    (Codex P2/NON-BLOCKING, #705, discussion_r3921525197).

    Fix: the smallest gap that repeats at least once. A single overrun is,
    by construction, a ONE-OFF -- it has no reason to recur -- so requiring
    a second occurrence filters it out the same way it filters out an
    outage: neither a lone fast fluke nor an outage's own width can win
    unless the run happened to repeat that width, and an outage that
    repeats enough times to pass this test is no longer distinguishable from
    a real (if slow) cadence with the information gaps alone carry. No gap
    repeats: UNKNOWN (0), not a guess.

    "Repeats" means WITHIN `CADENCE_JITTER_RATIO`, not to the microsecond.
    Demanding an exact integer match made this function return 0 for every
    real capture: `OpenDJProbe`'s sampling loop stamps each record AFTER a
    variable amount of collection work and sleeps against a monotonic
    deadline, so consecutive gaps always differ a little. The one real
    capture in this tree, `tests/fixtures/perf/probe-samples.jsonl`, is
    14.882s apart where 15s was asked for. Cadence 0 leaves every counter
    zero-width, which is precisely the false different-session warning this
    module exists to remove -- and no test caught it, because every fixture
    here spaces its samples to the exact microsecond (Codex P1/BLOCKING,
    #1611, discussion_r3970505604).
    """

    ordered = sorted(gaps)
    for index, smallest in enumerate(ordered):
        cluster = [gap for gap in ordered[index:] if gap <= smallest * CADENCE_JITTER_RATIO]
        if len(cluster) >= 2:
            return int(statistics.median(cluster))
    return 0


def _cadence_by_run(stamps: list[int], run_starts: list[int]) -> dict[int, int]:
    """Each stamp's cadence, from its own run's intervals only -- never another's."""

    by_ts: dict[int, int] = {}
    for index, start in enumerate(run_starts):
        end = run_starts[index + 1] if index + 1 < len(run_starts) else None
        run_stamps = [ts for ts in stamps if ts >= start and (end is None or ts < end)]
        gaps = [later - earlier for earlier, later in pairwise(run_stamps)]
        cadence = _agreeing_cadence(gaps) if len(run_stamps) >= 4 else 0
        by_ts.update(dict.fromkeys(run_stamps, cadence))
    return by_ts


LOAD_PATH_PREFIXES = ("deck-load", "deck-stems")


def _load_path_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ring rows that describe a track being loaded, and nothing else.

    `event_kind_family` truncates a kind at its sid, so the family is a prefix
    of the kind: a load span carries it as `name`, and phases carry it in
    `cat` as `<family>.phase`. An unmodeled load becomes an instant named for
    the family and still counts -- duration unknown, not absent. Either field
    matching is enough; a non-load row matches neither.
    """

    return [
        event
        for event in events
        if str(event.get("name", "")).startswith(LOAD_PATH_PREFIXES)
        or str(event.get("cat", "")).startswith(LOAD_PATH_PREFIXES)
    ]


def _closest_approach_us(
    left: list[tuple[int, int]], right: list[tuple[int, int]]
) -> int | None:
    """Smallest distance between any interval on one side and any on the other.

    Zero or negative means the layers genuinely meet; positive is the real gap.
    Two pointers, not the cross product it started as: both sides arrive
    merged and sorted, so the closest pair is always reached by advancing
    whichever interval ENDS first. The cross product allocated one integer
    per pair -- millions of temporaries for a question needing constant
    space. Codex found it on #705.
    """

    index_left = index_right = 0
    closest: int | None = None
    while index_left < len(left) and index_right < len(right):
        left_start, left_end = left[index_left]
        right_start, right_end = right[index_right]
        gap = max(left_start, right_start) - min(left_end, right_end)
        closest = gap if closest is None else min(closest, gap)
        if left_end <= right_end:
            index_left += 1
        else:
            index_right += 1
    return closest


def source_overlap(
    frontend: list[dict[str, Any]], native: list[dict[str, Any]]
) -> dict[str, Any]:
    """Do the two layers actually describe the same session?

    The ring is a 40-row window that can be WEEKS old while the probe log is
    from this morning. Merging those produces a technically correct trace
    covering thirteen days, in which the counters have nothing to do with the
    loads sitting at the far end of it. Nobody reading the picture would guess
    that, so it is stated rather than left to be noticed.
    """

    front = _window_us(frontend)
    back = _window_us(native)
    if front is None or back is None:
        return {"comparable": False, "reason": "one source contributed no events"}
    # Compared span by span, not envelope to envelope (envelopes are still
    # reported, for readers, not the verdict; see `_merged_spans_us`). A
    # layer can be a zero-wide instant, so the test is "is there a positive
    # GAP", not "is there positive overlap" -- the latter reports a sample
    # taken during a load as a different session.
    #
    # Compared against the LOADS, not every ring row: an unrelated row (an
    # `audio-context` device floor) logged beside the probe would otherwise
    # vouch for loads days away. Codex found it on #705. A ring with no
    # loads is answered from what it does hold, naming that basis.
    loads = _load_path_events(frontend)
    compared = loads if loads else frontend
    gap_us = _closest_approach_us(_merged_spans_us(compared), _sampled_spans_us(native))
    if gap_us is None:
        return {"comparable": False, "reason": "one source contributed no events"}
    return {
        "comparable": True,
        "compared": "load-path rows" if loads else "all ring rows (it holds no loads)",
        "overlaps": gap_us <= 0,
        "gap_seconds": round(gap_us / 1_000_000, 1) if gap_us > 0 else 0.0,
        "frontend_utc": [_iso(front[0]), _iso(front[1])],
        "native_utc": [_iso(back[0]), _iso(back[1])],
    }


def _iso(epoch_micros: int) -> str:
    return datetime.fromtimestamp(epoch_micros / 1_000_000, tz=UTC).isoformat()
