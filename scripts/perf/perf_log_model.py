"""Reading and classifying the frontend perf ring: the one place that knows
what a `stages` map MEANS.

Both perf entry points (`export_chrome_trace.py`, `amdahl_report.py`) consume
the same ring and would otherwise each re-derive the same three facts. Keeping
it here follows the register's standing convention 1 (perf logic lives in its
own module; entry points get a call site, not a formula).

FILE REQUIREMENTS (mini-PRD)

* R1 the ring is readable from a live WKWebView localStorage store AND from a
  plain JSON dump. Status: OK, run, works as expected.
  - if the store is copied without its `-wal` sidecar then a live session's
    rows are missing and the read returns an EMPTY ring while exiting 0
  - if the value is decoded as UTF-8 rather than UTF-16LE then json.loads
    raises rather than silently returning nothing
  - if `ItemTable` or the key is absent then the read raises, because zero rows
    and an unreadable store must not look the same
* R2 a `stages` map is classified, never summed blind. Status: OK, run, works
  as expected.
  - if `audioBytes` (5,059,178) were treated as milliseconds then one load
    would read as 84 minutes
  - if `fetchWall`, `totalBeforeSwap` and `total` were added to per-step
    durations then the total is counted three times
  - if a stage name this module has never seen appears then it is reported as
    unclassified rather than dropped
* R3 a deck load decomposes into wall-clock phases that re-add to its measured
  total. Status: OK, run, works as expected.
  - if the phases sum to more than `total` then the residual is negative and
    the report says so instead of clamping
  - if a load has no `total` (it failed) then no phase model is produced
  - if `decodeMix` and `stretchCreate` were added rather than maxed then the
    decode phase over-counts, because they run in one Promise.all

SOURCE OF TRUTH for every name below: the `time()` call sites in
`apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts` and the `PerfEvent`
interface in `apps/webui/frontend/src/lib/rb/perf-event-log.ts`. Nothing here
is invented; a stage this file does not list is reported, not guessed at.
"""

from __future__ import annotations

import json
import math
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# ----------------------------------------------------------------- constants

STORAGE_KEY = "mdt.perfEventLog"
"""localStorage key written by perf-event-log.ts."""

STORAGE_TABLE = "ItemTable"
"""WebKit's localStorage table name."""

NON_DURATION_STAGES = frozenset(
    {"audioBytes", "audioPrefetchHit", "stemmed", "deferredToStop"}
)
"""Keys inside `stages` that are NOT milliseconds.

`stages` is stringly overloaded today: `audioBytes` is a byte count,
`audioPrefetchHit` / `stemmed` / `deferredToStop` are 0/1 flags. The program
spec calls this unit ambiguity out and requires it fixed BEFORE any aggregator
averages bytes with milliseconds, so every aggregator here excludes them by
name rather than by a heuristic on magnitude.
"""

CUMULATIVE_STAGES = frozenset({"fetchWall", "totalBeforeSwap", "total", "failedAt"})
"""Keys that are a checkpoint since load start, not the cost of one step.

`stages.fetchWall = perfMs()` measures from `perfT0`, so it already contains
every earlier stage. Adding these to per-step durations counts the same
milliseconds two and three times over.
"""

FETCH_GROUP_STAGES = frozenset(
    {
        "getTrack",
        "fetchAudio",
        "fetchAudioCacheHit",
        "fetchAnlz",
        "anlzCacheHit",
        "fetchHotCues",
        # Older builds timed the stem probe inside the load fetch group. LAZY
        # STEMS moved it onto the deck-stems row, so both placements appear in
        # a real ring depending on which build wrote it.
        "probeStem",
    }
)
"""Members of the load's one `Promise.all` fetch group. Their WALL is `fetchWall`."""

DECODE_GROUP_STAGES = ("decodeMix", "stretchCreate")
"""Members of the decode `Promise.all`. Their wall is the MAX, never the sum."""

SERIAL_TAIL_STAGES = ("stretchLoad", "processorLatency")
"""Awaited one after another between the decode group and `totalBeforeSwap`."""

STEM_GROUP_WALL_STAGES = ("fetchStems", "decodeStems")
"""Each an elapsed WALL time around one `Promise.all` over the layout's
parts, not a sum of independent per-part work -- same shape as `fetchWall`.

Neither is treated as parallelizable. `fetchWall` earned that treatment only
after `deck_load_phase_model` gained real per-member timings to floor it at
its slowest concurrent request; no equivalent per-part breakdown exists here
(`stages` carries one `fetchStems` scalar, never one entry per stem), so there
is no measured floor to retain, only a wall to remove entirely if this were
marked parallelizable -- exactly the defect the `fetchWall` fix corrected one
group over. `decodeStems` additionally has AFFIRMATIVE evidence against
compression: the register documents WebKit's single decode thread as what
`decodeStems` (not `decodeMix`) actually bites on, so more workers do not
free the wall the way `Promise.all` alone would suggest. Codex found this on
#705, perf_log_model.py:102.
"""

STEM_SERIAL_STAGES = ("probeStem",)
"""One metadata request that must settle before any part can be fetched."""

STEM_SERIAL_CREATE_STAGES = ("stemProcessorCreate",)
"""Worklet creation, currently sequential and NOT part of the parallel term.

The register's own measured evidence is "4 serial worklet creates" (queue
Q7): today's implementation awaits one creation after another rather than
fanning them out, so this stage cannot shrink with more workers yet. Treating
it as parallelizable would let the Amdahl ceiling promise a speedup this
phase's current code cannot deliver. Kept as its own group, not folded into
`STEM_SERIAL_STAGES`, because the "why" is queue Q7 becoming solved (fan the
creates out), not "waits behind a probe" like `probeStem`; the two stages
will need to move independently. `STEM_GROUP_WALL_STAGES` is withheld from
the parallel term for a different reason -- no measured floor, not a known
one-at-a-time implementation -- so the three serial groups are kept apart
rather than merged into one.
"""

EAGER_STEM_STAGES = frozenset(STEM_GROUP_WALL_STAGES) | frozenset(STEM_SERIAL_CREATE_STAGES)
"""Stem durations a pre-LAZY-STEMS build recorded directly on the `deck-load`
row, before stem work moved onto its own `deck-stems` row.

`deck_load_phase_model` has no phase for these: it models `load()`'s current
fetch/decode/swap shape, not the retired eager path. Left unhandled, their ms
fall into the `unattributed-pre-swap` gap and get counted SERIAL, understating
the parallel fraction and ceiling for exactly the loads that did the most
parallel work. They are real `_KNOWN_STAGES` names -- legitimate on a
`deck-stems` row -- so nothing here looks unclassified by name alone; only the
`deck-load` context is wrong. See `unclassified_deck_stages(is_load=...)`.
"""

DECK_LOAD_KINDS = ("deck-load", "deck-load-fail")
STEM_KINDS = ("deck-stems", "deck-stems-none", "deck-stems-fail")

_KNOWN_STAGES = (
    NON_DURATION_STAGES
    | CUMULATIVE_STAGES
    | FETCH_GROUP_STAGES
    | frozenset(DECODE_GROUP_STAGES)
    | frozenset(SERIAL_TAIL_STAGES)
    | frozenset(STEM_GROUP_WALL_STAGES)
    | frozenset(STEM_SERIAL_STAGES)
    | frozenset(STEM_SERIAL_CREATE_STAGES)
    | frozenset({"total"})
)


class PerfLogUnreadable(RuntimeError):
    """The ring could not be read. Never raised for a ring that is merely empty."""


# ------------------------------------------------------------------- reading


def _copy_sqlite_with_sidecars(source: Path, into: Path) -> Path:
    """Snapshot the store through SQLite's own online backup API.

    Reading a LIVE store directly races WebKit's own writer. A plain file
    copy of the `.sqlite3` PLUS its `-wal`/`-shm` sidecars is not enough to
    fix that: WebKit can checkpoint or write a new WAL frame between the two
    (or three) separate `copyfile` calls, handing back a main file and a WAL
    sidecar that no longer agree, which reads as a truncated or unreadable
    store -- exactly like the "no such table: ItemTable" failure this
    function was written to avoid, just intermittent instead of guaranteed.
    `sqlite3.Connection.backup()` uses SQLite's own Online Backup API, which
    holds the source's read lock for the ENTIRE page-by-page copy and
    reconciles concurrent WAL activity internally, so the result is always a
    single consistent snapshot no matter what the live writer does mid-copy.
    """

    target = into / "localstorage.sqlite3"
    try:
        source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    except sqlite3.DatabaseError as exc:
        raise PerfLogUnreadable(f"{source}: {exc}") from exc
    try:
        target_conn = sqlite3.connect(str(target))
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    except sqlite3.DatabaseError as exc:
        raise PerfLogUnreadable(f"{source}: {exc}") from exc
    finally:
        source_conn.close()
    return target


def _decode_storage_value(raw: bytes) -> str:
    """WebKit stores localStorage strings as UTF-16LE; older rows can be UTF-8."""

    for encoding in ("utf-16-le", "utf-8"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if text.lstrip().startswith("["):
            return text
    raise PerfLogUnreadable(
        f"{STORAGE_KEY} value ({len(raw)} bytes) decoded as neither UTF-16LE nor "
        "UTF-8 JSON. It is not a perf ring."
    )


def read_localstorage_perf_log(store: Path) -> list[dict[str, Any]]:
    """The perf ring out of one WKWebView localStorage sqlite store."""

    if not store.exists():
        raise PerfLogUnreadable(f"no localStorage store at {store}")
    with tempfile.TemporaryDirectory(prefix="opendj-perf-store-") as scratch:
        copy = _copy_sqlite_with_sidecars(store, Path(scratch))
        connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                f"SELECT value FROM {STORAGE_TABLE} WHERE key = ?", (STORAGE_KEY,)
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise PerfLogUnreadable(f"{store}: {exc}") from exc
        finally:
            connection.close()
    if not rows:
        raise PerfLogUnreadable(
            f"{store} has no {STORAGE_KEY} row. This store belongs to a different "
            "origin or a session that never wrote a perf event."
        )
    return _parse_perf_log(_decode_storage_value(bytes(rows[0][0])), str(store))


def read_json_perf_log(path: Path) -> list[dict[str, Any]]:
    """The perf ring out of a plain JSON dump (`__mdtPerfLog()` pasted to disk)."""

    if not path.exists():
        raise PerfLogUnreadable(f"no perf log at {path}")
    return _parse_perf_log(path.read_text(encoding="utf-8"), str(path))


def _parse_perf_log(text: str, origin: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise PerfLogUnreadable(f"{origin}: perf ring is not JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise PerfLogUnreadable(
            f"{origin}: perf ring is {type(parsed).__name__}, expected a JSON array"
        )
    for index, row in enumerate(parsed):
        if not isinstance(row, dict) or not all(type(row.get(key)) is str for key in ("t", "kind")):
            raise PerfLogUnreadable(
                f"{origin}: row {index} is not a PerfEvent (needs string `t` and `kind`)"
            )
    return parsed


def _newest_mtime(store: Path) -> float:
    """The store's own mtime, or a sidecar's if that is newer.

    WebKit in WAL mode updates `-wal` on every commit but can leave the main
    `.sqlite3` file's mtime untouched until the next checkpoint, so sorting on
    the main file alone can rank a session that is actively being written to
    BELOW a stale store that was merely checkpointed more recently.
    """

    newest = store.stat().st_mtime
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(store) + suffix)
        if sidecar.exists():
            newest = max(newest, sidecar.stat().st_mtime)
    return newest


def find_localstorage_stores(webkit_root: Path) -> list[Path]:
    """Every localStorage store under a WebKit data root, newest write first.

    A machine accumulates one store per origin per bundle id, and this one has
    twenty. Callers still have to open them to learn which holds a perf ring;
    this only narrows the search and orders it usefully. "Newest" is the
    newest mtime across the store AND its `-wal`/`-shm` sidecars (see
    `_newest_mtime`), not the main file alone.
    """

    stores = list(webkit_root.glob("**/LocalStorage/localstorage.sqlite3"))
    return sorted(stores, key=_newest_mtime, reverse=True)


# ------------------------------------------------------------------ timebase


def epoch_us(stamp: str, origin: str) -> int:
    """A PerfEvent `t` or a probe `timestamp` as microseconds since the epoch.

    Both sources write `Date`/`datetime` ISO 8601 in UTC, so they land on ONE
    real timeline with no correlation step. Refuses rather than inventing a
    stamp: a trace whose events are placed at a fabricated time is worse than a
    trace that failed to build.
    """

    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise PerfLogUnreadable(
            f"{origin}: {stamp!r} is not an ISO 8601 timestamp, so its events "
            "cannot be placed on a timeline"
        ) from exc
    if moment.tzinfo is None:
        raise PerfLogUnreadable(
            f"{origin}: {stamp!r} carries no timezone. A naive stamp cannot be "
            "aligned with the other source without guessing the offset."
        )
    return round(moment.timestamp() * 1_000_000)


# ------------------------------------------------------------- stage helpers


def unclassified_deck_stages(
    stages: dict[str, Any], *, is_load: bool = False
) -> set[str]:
    """Load-path stage names the phase model has no declared meaning for.

    Reported by every caller instead of being dropped, because the ring is
    additive by design: a new `time()` call site inside `load()` must show up
    as a visible gap in the model rather than silently vanishing from the
    totals it is supposed to explain.

    SCOPED TO THE LOAD PATH ON PURPOSE. The ring also carries `library-*`,
    `worklet-ack`, `transport-schedule` and `audio-context` rows, each with its
    own stage vocabulary that this module makes no claim about; those rows are
    exported whole, as instants, and lose nothing. Flagging them here would
    make the warning fire constantly and mean nothing, which is how a real
    warning gets ignored. Callers pass only deck-load and deck-stems maps.

    `is_load=True` additionally flags `EAGER_STEM_STAGES`: real, known names
    on a `deck-stems` row, but a pre-LAZY-STEMS artifact on a `deck-load` row
    that `deck_load_phase_model` cannot place into a phase and would otherwise
    fold into `unattributed-pre-swap` without a word.
    """

    known = _KNOWN_STAGES - EAGER_STEM_STAGES if is_load else _KNOWN_STAGES
    return {name for name in stages if name not in known}


def load_sid(kind: str) -> str | None:
    """The stable-id fragment out of `deck-load sid=<12 hex>`, or None."""

    marker = " sid="
    index = kind.find(marker)
    return None if index < 0 else kind[index + len(marker) :].strip()


def event_kind_family(kind: str) -> str:
    """`deck-load sid=abc` -> `deck-load`. The sid makes every kind unique."""

    return kind.split(" sid=", maxsplit=1)[0]


# -------------------------------------------------------------- phase model


@dataclass(frozen=True)
class Phase:
    """One wall-clock slice of a load, and whether more workers could shrink it."""

    name: str
    wall_ms: float
    parallelizable: bool
    why: str


@dataclass(frozen=True)
class PhaseModel:
    """A load decomposed into phases that re-add to its measured wall time."""

    total_ms: float
    phases: tuple[Phase, ...]
    residual_ms: float
    """`total_ms` minus the phases. Non-zero means the model missed something.

    Surfaced rather than clamped: a negative residual says the phases
    over-count and the model is WRONG, which a max(0, ...) would hide.
    """

    contested_fetch_group: bool = False
    """True when the fetch wall's floor/parallel split cannot be trusted:
    either >1 member shares a single-worker engine, or no member breakdown
    exists at all. Caller must withhold this load's ceiling either way
    (Codex P1/BLOCKING, #705, perf_log_model.py:436 and :459)."""

    @property
    def parallelizable_ms(self) -> float:
        return sum(phase.wall_ms for phase in self.phases if phase.parallelizable)

    @property
    def serial_ms(self) -> float:
        return self.total_ms - self.parallelizable_ms


def deck_load_phase_model(stages: dict[str, Any]) -> PhaseModel | None:
    """Decompose one `deck-load` stage map. None when the load never finished.

    The ordering is read straight off `load()`: one fetch `Promise.all`, then a
    decode `Promise.all`, then two awaits, then the deck swap. Because
    `fetchWall` and `totalBeforeSwap` are checkpoints from `perfT0`, the phases
    below are differences between checkpoints, never a sum of member stages.
    """

    if "total" not in stages:
        return None
    total = float(stages["total"])
    phases: list[Phase] = []

    fetch_wall = stages.get("fetchWall")
    contested_fetch_group = False
    if fetch_wall is not None:
        fetch_wall_ms = float(fetch_wall)
        fetch_members = [
            float(stages[name]) for name in FETCH_GROUP_STAGES if name in stages
        ]
        if len(fetch_members) > 1:
            # `Promise.all` starts every member together, but they queue
            # behind the SAME single-worker engine, so a member's own
            # duration can include queueing, not just service time. Charge
            # the whole wall as one unsplittable phase, ceiling withheld
            # (Codex P1/BLOCKING, #705, perf_log_model.py:436).
            contested_fetch_group = True
            if fetch_wall_ms > 0:
                phases.append(
                    Phase(
                        "fetch-contested",
                        fetch_wall_ms,
                        False,
                        "multiple fetch members share a single-worker engine; "
                        "durations alone cannot separate queueing from service",
                    )
                )
        elif len(fetch_members) == 1:
            # A lone member's own duration IS the floor (#705, :418).
            floor_ms = min(fetch_members[0], fetch_wall_ms)
            if floor_ms > 0:
                phases.append(Phase("fetch-floor", floor_ms, False, "the lone fetch member"))
            remainder_ms = fetch_wall_ms - floor_ms
            if remainder_ms > 0:
                phases.append(
                    Phase("fetch-remainder", remainder_ms, False, "not concurrent work (#705)")
                )
        else:
            # No member breakdown: absence of proof is not proof of full
            # parallelism (Codex P1/BLOCKING, #705, perf_log_model.py:459).
            contested_fetch_group = True
            if fetch_wall_ms > 0:
                phases.append(
                    Phase("fetch-unmeasured", fetch_wall_ms, False, "no fetch-group breakdown")
                )

    decode_members = [
        float(stages[name]) for name in DECODE_GROUP_STAGES if name in stages
    ]
    if decode_members:
        phases.append(
            Phase(
                "decode-mix",
                max(decode_members),
                False,
                "one decodeAudioData of one buffer, overlapped with the worklet "
                "create; WebKit's single decode thread is not the limit here "
                "(register: it bites decodeStems, not the lone decodeMix)",
            )
        )

    phases.extend(
        Phase(name, float(stages[name]), False, "awaited on its own")
        for name in SERIAL_TAIL_STAGES
        if name in stages
    )

    before_swap = float(stages.get("totalBeforeSwap", total))
    accounted = sum(phase.wall_ms for phase in phases)
    gap = before_swap - accounted
    if gap > 0:
        phases.append(
            Phase(
                "unattributed-pre-swap",
                gap,
                False,
                "graph setup and scheduling between the timed steps",
            )
        )
    swap = total - before_swap
    if swap > 0:
        phases.append(Phase("deck-swap", swap, False, "the guarded processor swap"))

    return PhaseModel(
        total_ms=total,
        phases=tuple(phases),
        residual_ms=total - sum(phase.wall_ms for phase in phases),
        contested_fetch_group=contested_fetch_group,
    )


def stem_phase_model(stages: dict[str, Any]) -> PhaseModel | None:
    """Decompose one `deck-stems` stage map. None when the upgrade never finished.

    Every stage on this row is measured with its own `time()` wrapper and they
    are awaited in sequence, so unlike the load row these ARE additive.
    """

    if "total" not in stages:
        return None
    total = float(stages["total"])
    phases: list[Phase] = []
    phases.extend(
        Phase(
            name,
            float(stages[name]),
            False,
            "one manifest probe; every part waits behind it",
        )
        for name in STEM_SERIAL_STAGES
        if name in stages
    )
    phases.extend(
        Phase(
            name,
            float(stages[name]),
            False,
            "a Promise.all wall over the layout's parts, not a per-part "
            "breakdown; no measured floor to retain, so withheld from the "
            "parallel term rather than removed whole (Codex on #705)",
        )
        for name in STEM_GROUP_WALL_STAGES
        if name in stages
    )
    phases.extend(
        Phase(
            name,
            float(stages[name]),
            False,
            "4 serial worklet creates today (register queue Q7); not yet "
            "fanned out, so more workers cannot shrink it",
        )
        for name in STEM_SERIAL_CREATE_STAGES
        if name in stages
    )
    accounted = sum(phase.wall_ms for phase in phases)
    if total - accounted > 0:
        phases.append(
            Phase(
                "unattributed-stems",
                total - accounted,
                False,
                "state settling and the deck swap between the timed steps",
            )
        )
    return PhaseModel(
        total_ms=total,
        phases=tuple(phases),
        residual_ms=total - sum(phase.wall_ms for phase in phases),
    )


# ------------------------------------------------------------- amdahl's law


def amdahl_speedup(parallel_fraction: float, workers: float) -> float:
    """Speedup when `parallel_fraction` of the wall time goes `workers`-way wide.

    `workers` may be `math.inf`, which is the ceiling: the best any amount of
    parallelism can do, because the serial part is still there.
    """

    if not 0.0 <= parallel_fraction <= 1.0:
        raise ValueError(f"parallel fraction {parallel_fraction} is not in 0..1")
    if workers < 1:
        raise ValueError(f"worker count {workers} is below 1")
    serial = 1.0 - parallel_fraction
    if serial == 0.0 and workers == math.inf:
        # An entirely parallel load (fraction == 1.0) at infinite workers: the
        # serial term and parallel/workers term are both exactly zero, so the
        # closed form below divides by zero. The true limit is infinite
        # speedup, not an error -- there is no serial floor to hit.
        return math.inf
    return 1.0 / (serial + parallel_fraction / workers)
