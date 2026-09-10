"""Ring-classification primitives from perf_log_model.py that amdahl_report.py
and export_chrome_trace.py both build on. See that module's own docstring for
the invariants (stage classification, phase decomposition, timebase).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

import pytest

from scripts.perf.perf_log_model import (
    PerfLogUnreadable,
    deck_load_phase_model,
    find_localstorage_stores,
    read_localstorage_perf_log,
    stem_phase_model,
    unclassified_deck_stages,
)


def _touch(path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))


def test_stores_sort_by_wal_activity_not_just_the_main_file(tmp_path) -> None:
    """If broken: an actively-written WAL session ranks below a stale store.

    WebKit checkpoints the main `.sqlite3` file lazily, so a session that is
    live right now can have an OLD main-file mtime while its `-wal` sidecar
    is brand new. Sorting on the main file alone would rank the stale store
    (whose owner last checkpointed recently, e.g. on quit) above the live one.
    """
    now = time.time()

    stale_main = tmp_path / "origin-a" / "LocalStorage" / "localstorage.sqlite3"
    _touch(stale_main, now - 10)  # checkpointed 10s ago, no WAL activity since

    live_main = tmp_path / "origin-b" / "LocalStorage" / "localstorage.sqlite3"
    _touch(live_main, now - 3600)  # main file is an HOUR stale
    _touch(tmp_path / "origin-b" / "LocalStorage" / "localstorage.sqlite3-wal", now - 1)

    stores = find_localstorage_stores(tmp_path)
    assert stores[0] == live_main
    assert stores[1] == stale_main


def test_a_store_with_no_sidecars_sorts_on_its_own_mtime(tmp_path) -> None:
    """If broken: a checkpointed-clean store with no `-wal` crashes or misorders."""
    now = time.time()
    older = tmp_path / "origin-a" / "LocalStorage" / "localstorage.sqlite3"
    _touch(older, now - 100)
    newer = tmp_path / "origin-b" / "LocalStorage" / "localstorage.sqlite3"
    _touch(newer, now - 1)

    stores = find_localstorage_stores(tmp_path)
    assert stores == [newer, older]


def _perf_row(n: int) -> dict[str, object]:
    return {"t": "2026-09-01T00:00:00.000Z", "kind": f"deck-load sid={n:012x}", "deck": n % 4}


def test_read_survives_concurrent_wal_writes(tmp_path) -> None:
    """If broken: a snapshot taken mid-write is truncated or unreadable.

    A file-by-file copy of the main db plus its `-wal`/`-shm` sidecars can
    race a live writer BETWEEN the separate `copyfile` calls: WebKit can
    checkpoint or extend the WAL in that gap, handing back a main file and
    WAL that disagree. `_copy_sqlite_with_sidecars` now goes through SQLite's
    own online backup API instead, which holds the source's read lock for the
    whole page-by-page copy, so every snapshot must stay internally
    consistent no matter what a concurrent writer does mid-copy.
    """
    store = tmp_path / "localstorage.sqlite3"
    setup = sqlite3.connect(store)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
    setup.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        ("mdt.perfEventLog", json.dumps([_perf_row(0)]).encode("utf-16-le")),
    )
    setup.commit()
    setup.close()

    stop = threading.Event()
    failures: list[BaseException] = []

    def writer() -> None:
        connection = sqlite3.connect(store)
        connection.execute("PRAGMA journal_mode=WAL")
        n = 0
        try:
            while not stop.is_set():
                n += 1
                payload = json.dumps([_perf_row(i) for i in range(n, n + 5)]).encode(
                    "utf-16-le"
                )
                connection.execute(
                    "UPDATE ItemTable SET value = ? WHERE key = 'mdt.perfEventLog'",
                    (payload,),
                )
                connection.commit()
                if n % 10 == 0:
                    connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            connection.close()

    writer_thread = threading.Thread(target=writer, daemon=True)
    writer_thread.start()
    reads = 0
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                rows = read_localstorage_perf_log(store)
            except (sqlite3.DatabaseError, PerfLogUnreadable) as exc:
                failures.append(exc)
                continue
            assert isinstance(rows, list) and rows, "a live writer never empties the row"
            reads += 1
    finally:
        stop.set()
        writer_thread.join(timeout=5)

    assert reads > 0, "the writer never got a chance to race a read"
    assert failures == [], (
        f"{len(failures)} of {reads + len(failures)} reads failed under "
        f"concurrent WAL writes: {failures[:3]}"
    )


def test_no_stem_stage_is_counted_as_parallelizable_yet() -> None:
    """If broken: the Amdahl ceiling promises a speedup no current code path
    can deliver. `stemProcessorCreate` is today's "4 serial worklet creates"
    (register queue Q7). `fetchStems`/`decodeStems` are each one `Promise.all`
    WALL with no per-part breakdown to floor at a slowest member, and
    `decodeStems` additionally bites WebKit's single decode thread -- marking
    either removable-to-zero overstated 9000 of 10400ms as achievable on the
    locked mix-load capture (Codex P1/BLOCKING finding on PR #705).
    """
    model = stem_phase_model(
        {
            "probeStem": 30,
            "fetchStems": 4000,
            "decodeStems": 5000,
            "stemProcessorCreate": 1350,
            "total": 10400,
        }
    )
    assert model is not None
    by_name = {phase.name: phase for phase in model.phases}
    assert by_name["stemProcessorCreate"].parallelizable is False
    assert by_name["fetchStems"].parallelizable is False
    assert by_name["decodeStems"].parallelizable is False
    assert model.parallelizable_ms == pytest.approx(0)


def test_eager_stem_stages_are_unclassified_on_a_deck_load_row() -> None:
    """If broken: a pre-LAZY-STEMS deck-load row's stem work folds silently
    into `unattributed-pre-swap` (SERIAL) with no anomaly warning, because
    `fetchStems`/`decodeStems`/`stemProcessorCreate` are real `_KNOWN_STAGES`
    names -- just not ones `deck_load_phase_model` has a phase for on a
    deck-load row (Codex P1/BLOCKING finding on PR #705, discussion_r3910193388).
    """
    stages = {
        "fetchWall": 200,
        "totalBeforeSwap": 9600,
        "total": 9800,
        "stemmed": 1,
        "fetchStems": 4000,
        "decodeStems": 5000,
        "stemProcessorCreate": 1350,
    }
    assert unclassified_deck_stages(stages, is_load=True) == {
        "fetchStems",
        "decodeStems",
        "stemProcessorCreate",
    }


def test_eager_stem_stages_stay_classified_on_a_deck_stems_row() -> None:
    """If broken: tightening `is_load=True` also breaks the normal, current
    `deck-stems` row shape, which legitimately carries these same names.
    """
    stages = {
        "probeStem": 30,
        "fetchStems": 4000,
        "decodeStems": 5000,
        "stemProcessorCreate": 1350,
        "total": 10400,
    }
    assert unclassified_deck_stages(stages, is_load=False) == set()
    assert unclassified_deck_stages(stages) == set()


def test_a_contested_fetch_wall_is_wholly_serial_not_floor_split() -> None:
    """If broken: a fetch group with several concurrent members through one
    single-worker engine still gets a floor/parallel split, computed from
    durations that can each include time queued behind a sibling, not just
    their own service time -- an unproven number presented as a measured one.

    A prior fix (superseded here) floored the wall at its slowest member and
    called the rest parallel, on the theory more workers compress the wall
    toward that member. That still assumes each member's OWN duration is
    pure service time; with several members sharing one connection pool, a
    member's duration can include queueing behind its siblings, so neither
    the floor nor the "parallel" remainder is trustworthy. Codex found this
    one level up on #705 (perf_log_model.py:436): withhold the split, don't
    guess it.
    """
    model = deck_load_phase_model(
        {
            "getTrack": 70,
            "fetchHotCues": 172,
            "fetchAnlz": 621,
            "fetchAudio": 778,
            "fetchWall": 782,
            "totalBeforeSwap": 782,
            "total": 782,
        }
    )
    assert model is not None
    assert model.contested_fetch_group is True
    by_name = {phase.name: phase for phase in model.phases}
    assert by_name["fetch-contested"].wall_ms == pytest.approx(782)
    assert by_name["fetch-contested"].parallelizable is False
    assert model.parallelizable_ms == pytest.approx(0)


def test_a_fetch_wall_with_no_member_breakdown_withholds_the_ceiling() -> None:
    """If broken: a row with no fetch-group members either crashes on an
    empty max(), or (the actual prior bug, Codex P1/BLOCKING, #705,
    perf_log_model.py:459) defaults the whole wall to parallelizable for lack
    of a floor -- absence of proof read as proof of full parallelism, an
    unmeasured wall reporting a confident, possibly-infinite ceiling.

    Some rows carry `fetchWall` with none of `FETCH_GROUP_STAGES` present (a
    synthetic/partial capture). With no measured member there is no evidence
    of a floor OR of parallelism, so this gets the same conservative
    treatment as a contested multi-member group: non-parallelizable, ceiling
    withheld.
    """
    model = deck_load_phase_model(
        {"fetchWall": 90, "totalBeforeSwap": 90, "total": 90}
    )
    assert model is not None
    assert model.contested_fetch_group is True
    by_name = {phase.name: phase for phase in model.phases}
    assert "fetch-floor" not in by_name
    assert "fetch-parallel" not in by_name
    assert by_name["fetch-unmeasured"].wall_ms == pytest.approx(90)
    assert by_name["fetch-unmeasured"].parallelizable is False
    assert model.parallelizable_ms == pytest.approx(0)


def test_a_lone_fetch_members_remainder_is_not_credited_as_parallel() -> None:
    """If broken: a lone fetch member shorter than `fetchWall` has its
    unexplained remainder classified as parallelizable, publishing an Amdahl
    speedup no fan-out could have produced -- a single measured request has
    nothing concurrent to overlap, so the gap is scheduling or uninstrumented
    time, not removable work. A zero-duration member against a large wall
    (exercised here) would otherwise yield an arbitrarily large unsupported
    ceiling (Codex P1/BLOCKING, #705, perf_log_model.py:462).

    Unlike the two siblings above, this is NOT a contested/unmeasured group:
    the lone member still gives a real floor, so `contested_fetch_group`
    stays False and the load's ceiling is not withheld -- it is simply zero.
    """
    model = deck_load_phase_model(
        {"fetchWall": 90000, "fetchAudio": 0, "totalBeforeSwap": 90000, "total": 90000}
    )
    assert model is not None
    assert model.contested_fetch_group is False
    by_name = {phase.name: phase for phase in model.phases}
    assert "fetch-parallel" not in by_name
    assert by_name["fetch-remainder"].wall_ms == pytest.approx(90000)
    assert by_name["fetch-remainder"].parallelizable is False
    assert model.parallelizable_ms == pytest.approx(0)
