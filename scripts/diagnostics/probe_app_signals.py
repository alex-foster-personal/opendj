"""In-app signals the OS counters cannot see: engine HTTP and the browser ring.

Both are best-effort by design. A probe that dies because the engine is down,
or because a WebKit LocalStorage file is mid-write, would stop producing the
process footprints that are its actual job. The one write that must NOT be
best-effort, the RED-trend report, lives in ``probe_trend_report``.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .probe_types import ProcessRow

TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
PERF_RING_STORAGE_KEY = "mdt.perfEventLog"
DECK_STATE_STORAGE_KEY = "mdt.deckState"
DECK_LOAD_COMPLETED_KIND = "deck-load"
DECK_STATE_EMPTY_KINDS = frozenset({"deck-state-empty", "deck-unload"})
DECK_LOAD_RING_BUDGET = 16
"""Mirrors perf-event-log.ts DECK_LOAD_BUDGET. The ring FIFO-evicts the oldest
`deck-load`-bucket row (a completion or a `deck-load-fail`) once this many are
already held, so at budget a deck's completed-load row may be gone even though
the deck is still loaded. Below budget nothing in that bucket has ever been
evicted, so a bucket count under budget is reliable evidence.
"""
DECK_STATE_RING_BUDGET = 8
"""Mirrors perf-event-log.ts DECK_STATE_BUDGET, the SEPARATE FIFO budget for the
`deck-unload` / `deck-state-empty` bucket. Independent of DECK_LOAD_RING_BUDGET:
a flood of loads cannot evict an unload row and vice versa, but each bucket can
still evict its OWN oldest rows once it is at its own budget.
"""
DECK_STATE_BUCKETS: dict[str, int] = {
    "deck-load": DECK_LOAD_RING_BUDGET,
    "deck-state": DECK_STATE_RING_BUDGET,
}
"""The two perf-ring buckets that decide deck load/unload state, each with its
own FIFO budget. `other` and `transport-schedule` rows never bear on deck
state and are ignored here."""


ENGINE_OBSERVATION_TIMEOUT_SECONDS = 1.5
"""Per-request wait for the SAMPLER's engine reads (``engine_metrics``).

Short on purpose: those reads are observations taken inside the 15-second
sampling loop, three per sample, and a slow engine must cost a sample one
missing field rather than stall the footprints that are the loop's job. The
RED-trend report is not an observation and does not use this number.
"""


def fetch_json(url: str, timeout: float) -> Any:
    """GET ``url`` as JSON. The wait is the caller's decision, never a default."""

    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _job_rollup(jobs: list[Any]) -> dict[str, Any]:
    """Counts plus the still-running jobs, so a sample explains its own CPU."""

    statuses = Counter(
        str(item.get("status", "unknown")) for item in jobs if isinstance(item, dict)
    )
    return {
        "count": len(jobs),
        "by_status": dict(statuses),
        "active": [
            {
                "id": item.get("id"),
                "kind": item.get("kind"),
                "status": item.get("status"),
                "worker_pid": item.get("worker_pid"),
            }
            for item in jobs
            if isinstance(item, dict) and item.get("status") not in TERMINAL_JOB_STATUSES
        ],
    }


def engine_metrics(family: list[tuple[ProcessRow, str]]) -> dict[str, Any]:
    engine = next((row for row, role in family if role == "python-engine"), None)
    if engine is None:
        return {"available": False, "reason": "python engine not found"}
    port_match = re.search(r"(?:^|\s)--port\s+(\d+)(?:\s|$)", engine.command)
    if port_match is None:
        return {"available": False, "reason": "engine port not found", "pid": engine.pid}
    port = int(port_match.group(1))
    base = f"http://127.0.0.1:{port}/api/v1"
    result: dict[str, Any] = {"available": True, "pid": engine.pid, "port": port}
    for name, path in (
        ("health", "/health"),
        ("jobs", "/jobs"),
        ("clients", "/telemetry/clients"),
    ):
        try:
            value = fetch_json(base + path, ENGINE_OBSERVATION_TIMEOUT_SECONDS)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            result[name] = {"error": type(exc).__name__}
            continue
        result[name] = _job_rollup(value) if name == "jobs" and isinstance(value, list) else value
    return result


def _is_completed_deck_load(kind: str) -> bool:
    """True only for a load that actually FINISHED.

    audio-engine.svelte.ts writes `deck-load sid=<id>` once the deck can play,
    and `deck-load-fail` from the load catch block. A `deck-load` PREFIX test
    therefore reads a failure as a load, so four failed attempts would
    fabricate a four-deck loaded state. Allowlist the completion instead of
    denylisting the failure kinds that exist today: a `deck-load-*` kind added
    later is then ignored until it is named here, rather than silently read as
    a completed load.
    """

    return kind == DECK_LOAD_COMPLETED_KIND or kind.startswith(DECK_LOAD_COMPLETED_KIND + " ")


def _decode_local_storage_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes):
        return str(value)
    if len(value) >= 2 and value[1] == 0:
        return value.decode("utf-16-le")
    return value.decode("utf-8")


def _local_storage_candidates(bundle_ids: Iterable[str]) -> list[Path]:
    found: list[Path] = []
    for bundle_id in bundle_ids:
        root = Path.home() / "Library/WebKit" / bundle_id / "WebsiteData/Default"
        try:
            found.extend(root.glob("*/*/LocalStorage/localstorage.sqlite3"))
        except OSError:
            continue
    return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)


def _read_storage_json(path: Path, key: str) -> Any | None:
    """One LocalStorage key from a WebKit sqlite file, JSON-decoded, or None."""

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.25)
        try:
            row = connection.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                (key,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return json.loads(_decode_local_storage_value(row[0]))
    except (OSError, sqlite3.Error, UnicodeError, ValueError, TypeError):
        return None


def _read_perf_ring(path: Path) -> list[Any] | None:
    """The perf event ring from one LocalStorage file, or None if unreadable."""

    events = _read_storage_json(path, PERF_RING_STORAGE_KEY)
    if not isinstance(events, list) or not events:
        return None
    return events


def _read_empty_deck_baseline(path: Path) -> dict[str, Any] | None:
    """The frontend's 'four decks were created empty' stamp, or None.

    perf-event-log writes it once per page load to its own localStorage key
    (mdt.deckState) instead of four ring rows, because the initial deck state is
    a constant and spending a shared-budget ring row restating it on every load
    evicted real diagnostics. The probe needs the stamp to read an idle deck as
    UNLOADED rather than as missing evidence.
    """

    stamp = _read_storage_json(path, DECK_STATE_STORAGE_KEY)
    if not isinstance(stamp, dict):
        return None
    decks = stamp.get("unloaded")
    if (
        stamp.get("v") != 1
        or not isinstance(decks, list)
        or not decks
        or not all(isinstance(deck, int) and 1 <= deck <= 4 for deck in decks)
        or not isinstance(stamp.get("t"), str)
    ):
        return None
    return stamp


def _newest_perf_ring(bundle_ids: Iterable[str]) -> tuple[Path, list[Any]] | None:
    """Newest-write-wins across every candidate bundle id's storage file."""

    best: tuple[str, Path, list[Any]] | None = None
    for path in _local_storage_candidates(bundle_ids):
        events = _read_perf_ring(path)
        if events is None:
            continue
        last = events[-1]
        last_timestamp = str(last.get("t", "")) if isinstance(last, dict) else ""
        if best is None or last_timestamp > best[0]:
            best = (last_timestamp, path, events)
    return None if best is None else (best[1], best[2])


def _perf_bucket_of(kind: str) -> str:
    """Which FIFO bucket a ring row's SURVIVAL depends on, mirroring
    perf-event-log.ts `_bucketOf` as far as deck state cares: `deck-load`
    covers a completion and `deck-load-fail`; `deck-state` covers the legacy
    `deck-state-empty` boot rows and `deck-unload`. Everything else pools into
    `other`, which never bears on deck-state trust.
    """

    if kind.startswith(DECK_LOAD_COMPLETED_KIND):
        return "deck-load"
    if kind.startswith("deck-state") or kind == "deck-unload":
        return "deck-state"
    return "other"


def _post_gate_events(events: list[Any], gate: str | None) -> list[dict[str, Any]]:
    """Ring rows at or after the baseline instant, or every dict row if there
    is no baseline (an older build with no gate concept)."""

    return [
        event
        for event in events
        if isinstance(event, dict) and not (isinstance(gate, str) and str(event.get("t", "")) < gate)
    ]


def _bucket_eviction_horizon(post_gate_events: list[dict[str, Any]], bucket: str) -> str | None:
    """The oldest surviving post-gate timestamp in `bucket`, or None if the
    bucket holds fewer post-gate rows than its own FIFO budget.

    Below budget, nothing in this bucket has been evicted since the gate, so
    there is no eviction risk from it at all. At or over budget, eviction is
    active and oldest-first: any row that ever existed in this bucket, from
    ANY deck, older than the oldest row still standing here has been evicted -
    that oldest-surviving timestamp is the exact cutoff a deck's last-known
    state must clear to be trusted against this bucket.
    """

    timestamps = sorted(
        str(event.get("t", ""))
        for event in post_gate_events
        if _perf_bucket_of(str(event.get("kind", ""))) == bucket
    )
    if len(timestamps) < DECK_STATE_BUCKETS[bucket]:
        return None
    return timestamps[0]


def _deck_states(
    post_gate_events: list[dict[str, Any]], baseline: dict[str, Any] | None
) -> tuple[dict[int, bool], list[int]]:
    """Per-deck loaded state, trusted only when no state-changing row for that
    deck could have been silently evicted.

    INVARIANT: a deck's state is published only when its most recent
    state-changing row (a completed load OR an unload/empty-boot row) is
    PROVEN to survive - either in the baseline (a claim about the gate
    instant) or as a ring row at or after it. `deck-load` and `deck-state` are
    separate FIFO buckets with separate budgets (16 and 8), so a deck's
    surviving row can come from either one while the OTHER bucket has since
    evicted a newer row for the same deck - e.g. a stale `deck-unload` survives
    the 8-row deck-state bucket while the deck's later completed load was
    evicted from the 16-row deck-load bucket, which would invert a still-loaded
    deck into a fabricated clean unload if last-observation-wins were trusted
    blindly (issue #1403 round 4). This function checks the OTHER bucket's
    eviction horizon against the deck's own last-observed instant before
    trusting it, rather than accumulating a special case per discovered path.

    Per deck: take the most recent post-gate row across BOTH buckets (or the
    baseline's gate instant with state False, if the deck has no post-gate row
    yet). That row is safe from its OWN bucket's eviction - eviction is
    oldest-first, so a newer same-deck row in the SAME bucket would still be
    present if an older one is. It is NOT safe from the OTHER bucket: if that
    bucket is at or over its budget and its oldest-surviving row is newer than
    this deck's last-observed instant, a same-deck row in that bucket, newer
    than what we trust, could have been evicted without a trace - the deck's
    state is then UNKNOWN, not the value last observed. A baseline-only deck
    (no post-gate row from either bucket) is checked against BOTH bucket
    horizons, since neither bucket sourced its state.
    """

    horizons = {
        bucket: _bucket_eviction_horizon(post_gate_events, bucket) for bucket in DECK_STATE_BUCKETS
    }

    # last_observed[deck] = (timestamp, state, source_bucket); source_bucket is
    # None for a baseline-seeded deck, since neither ring bucket produced it.
    last_observed: dict[int, tuple[str, bool, str | None]] = {}
    if baseline is not None:
        gate: str = str(baseline["t"])
        for deck in baseline["unloaded"]:
            last_observed[deck] = (gate, False, None)

    for event in post_gate_events:
        if not isinstance(event.get("deck"), int):
            continue
        deck = event["deck"]
        kind = str(event.get("kind", ""))
        event_t = str(event.get("t", ""))
        event_bucket: str
        event_state: bool
        if _is_completed_deck_load(kind):
            event_bucket, event_state = "deck-load", True
        elif kind in DECK_STATE_EMPTY_KINDS:
            event_bucket, event_state = "deck-state", False
        else:
            # A failed or unrecognized load says nothing about what the deck
            # holds, so it neither updates nor is trusted as the deck's state.
            continue
        current = last_observed.get(deck)
        if current is None or event_t >= current[0]:
            last_observed[deck] = (event_t, event_state, event_bucket)

    states: dict[int, bool] = {}
    evicted_decks: list[int] = []
    for deck, (last_t, last_state, last_bucket) in last_observed.items():
        untrustworthy = any(
            bucket != last_bucket and horizon is not None and horizon > last_t
            for bucket, horizon in horizons.items()
        )
        if untrustworthy:
            evicted_decks.append(deck)
            continue
        states[deck] = last_state
    return states, sorted(evicted_decks)


def browser_perf_ring(bundle_ids: Iterable[str]) -> dict[str, Any]:
    newest = _newest_perf_ring(bundle_ids)
    if newest is None:
        return {"available": False, "reason": "perf ring not found"}
    path, events = newest
    baseline = _read_empty_deck_baseline(path)
    gate = baseline.get("t") if baseline is not None else None
    last = events[-1] if isinstance(events[-1], dict) else {}
    # loads, deck_load_count_in_ring and last_deck_load are gated the SAME way
    # as deck_state below: an ungated deck-load count next to a gated
    # loaded_deck_count mixes two different sessions' windows into one
    # response (issue #1403 round-4 P3), e.g. reporting a prior session's
    # last_deck_load as if it were this session's.
    gated_events = _post_gate_events(events, gate)
    loads = [
        event
        for event in gated_events
        if str(event.get("kind", "")).startswith("deck-load")
    ]
    deck_state, evicted_decks = _deck_states(gated_events, baseline)
    loaded_deck_count = sum(deck_state.values()) if len(deck_state) == 4 else None
    return {
        "available": True,
        "storage_path": str(path),
        "event_count": len(events),
        "first_timestamp": events[0].get("t") if isinstance(events[0], dict) else None,
        "last_timestamp": last.get("t"),
        "last_kind": last.get("kind"),
        "deck_load_count_in_ring": len(loads),
        # loaded_deck_count is 0..4 only when every deck's state is KNOWN: the
        # empty-deck baseline names all four, and rows refine from there. A
        # bounded ring can still omit a deck's most recent state, and a ring
        # with no baseline and no row for a deck has no evidence either way - so
        # None is reserved for that missing evidence, never published as a count.
        "loaded_deck_count": loaded_deck_count,
        # Set only when loaded_deck_count is None BECAUSE a deck's latest row
        # could have been evicted (as opposed to a deck with no evidence at
        # all, which the trend layer already names as "no deck-state evidence").
        "deck_state_reason": (
            "evicted:" + ",".join(f"deck{deck}" for deck in evicted_decks)
            if evicted_decks
            else None
        ),
        "last_deck_load": loads[-1] if loads else None,
    }
