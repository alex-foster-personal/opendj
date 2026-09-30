"""A hub pull page must not cost more in a bigger library (LIBM-120 L6).

Two per-row costs made a 10,000-track first sync slow:

* every :class:`HeldKeys` (one per hub pull page) elected the WHOLE library
  to learn which tracks lose an identity election: quadratic in the library;
* every row asked ``PRAGMA table_info`` for its table's columns.

The instruments observe the unmodified path from outside: SQLite's own VM
steps (``set_progress_handler`` at granularity 1), the statements it traces,
and Python's own call events (``sys.setprofile``), not wall time, so the
verdict does not move with host load. Every library is written through a
production write path: ``hub_apply`` on the hub, ``StateWriter`` and
``apply_hub_identity_rejects`` on a spoke.

[if] a hub pull page's work grows with the library [then] per-page identity election, [else stop].

Controls: a library whose tracks store the blank ``content_hash`` sync code
treats as missing must cost a page no more than a canonical one, in SQL or in
Python; per-row verdicts must equal the library-wide election on randomized
libraries with odd stored keys, deletions and persisted remap chains, both
before and after a walk switches to electing the library; the column memo
must never answer for another connection, nor outlive its scope.
"""
from __future__ import annotations

import random
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

import pytest

from apps.shared.state.writer import StateWriter
from apps.sync_hub import engine, identity_verdicts, protocol, sync_set
from apps.sync_hub.engine_identity_map import (
    apply_hub_identity_rejects,
    effective_identity_remap,
)
from tests.cloudsync.test_hub_sync import _DEV_A, _T0
from tests.cloudsync.test_track_identity_collapse import _open_hub, _values
from tests.cloudsync.test_track_identity_lookup_scale import _digest, _incoming, _isrc

pytestmark = pytest.mark.requirement("CLOUDSYNC-07")

SMALL_LIBRARY = 100
LARGE_LIBRARY = 2_000
MAX_GROWTH = 3.0
"""Per-row verdicts are index seeks: flat at 20x the library. A library-wide
election grows with it, about 20x here, and so does a fallback that files
every blank-hash row under every key a page seeks (11x in SQL, 18x in Python
measured Wed 30 Sep 2026 before that fix)."""


# ----- growth guard -------------------------------------------------------------


@dataclass(frozen=True)
class PageCost:
    vm_steps: int
    python_calls: int


LibraryShape = Callable[[int], str]
LIBRARY_SHAPES: dict[str, LibraryShape] = {
    "distinct-hashes": lambda i: _digest("c", i),
    "blank-content-hash": lambda i: "",
}
"""``content_hash`` of stored track ``i``; every track keeps a unique audio_hash."""


@contextmanager
def _counting_page_cost(conn: sqlite3.Connection) -> Iterator[list[int]]:
    """``[vm_steps, python_calls]`` spent inside the block, observed from outside."""
    counts = [0, 0]

    def _count_step() -> int:
        counts[0] += 1
        return 0

    def _count_call(frame: FrameType, event: str, arg: object) -> None:
        if event == "call":
            counts[1] += 1

    previous = sys.getprofile()
    conn.set_progress_handler(_count_step, 1)
    sys.setprofile(_count_call)
    try:
        yield counts
    finally:
        sys.setprofile(previous)
        conn.set_progress_handler(None, 1)


def _stored_library(
    conn: sqlite3.Connection, count: int, content_hash: LibraryShape
) -> list[protocol.RowChange]:
    return [
        protocol.RowChange(
            table="tracks",
            pk=(f"stored-{i}",),
            values=_values(
                conn,
                "tracks",
                stable_id=f"stored-{i}",
                stable_id_tier="fingerprint",
                title=f"stored {i}",
                isrc=_isrc("SEE", i),
                content_hash=content_hash(i),
                audio_hash=_digest("a", i),
                created_at=_T0,
                updated_at=_T0,
                origin_device_id=_DEV_A,
                deleted_at=None,
            ),
        )
        for i in range(count)
    ]


def _pull_page_cost(tmp_path: Path, count: int, shape: str) -> PageCost:
    """Cost to serve one pull page of new tracks from a hub holding ``count``."""
    conn = _open_hub(tmp_path / f"{shape}-{count}")
    try:
        library = _stored_library(conn, count, LIBRARY_SHAPES[shape])
        engine.hub_apply(conn, library)
        conn.commit()
        stored = conn.execute(
            "SELECT count(*) FROM tracks WHERE content_hash = ?",
            (LIBRARY_SHAPES[shape](0),),
        ).fetchone()[0]
        assert stored >= 1, f"the hub did not store the {shape} library as sent"
        since = engine.current_seq(conn)
        engine.hub_apply(conn, _incoming(conn))
        conn.commit()
        with _counting_page_cost(conn) as counts:
            batch = engine.hub_changes_since(conn, since)
        assert len(batch.rows) == len(_incoming(conn)), batch
        return PageCost(vm_steps=counts[0], python_calls=counts[1])
    finally:
        conn.close()


@pytest.mark.parametrize("shape", sorted(LIBRARY_SHAPES))
def test_a_pull_page_costs_the_same_in_a_bigger_library(tmp_path: Path, shape: str) -> None:
    small = _pull_page_cost(tmp_path, SMALL_LIBRARY, shape)
    large = _pull_page_cost(tmp_path, LARGE_LIBRARY, shape)
    assert small.vm_steps > 0 and small.python_calls > 0, (
        f"the instruments counted nothing ({small}); they measured no page"
    )
    growth = {
        "VM steps": large.vm_steps / small.vm_steps,
        "Python calls": large.python_calls / small.python_calls,
    }
    assert max(growth.values()) <= MAX_GROWTH, (
        f"one pull page against a {shape} library cost {small} with "
        f"{SMALL_LIBRARY} stored tracks and {large} with {LARGE_LIBRARY} "
        f"({', '.join(f'{name} {ratio:.1f}x' for name, ratio in growth.items())}). "
        "Something on the hub pull path reads or filters the whole tracks table "
        "per page (LIBM-120 L6)."
    )


# ----- verdict equivalence ------------------------------------------------------

_KEYS = tuple(f"k{i:02d}" for i in range(24))
_ODD_FORMS: tuple[Callable[[str], str], ...] = (
    lambda key: f" {key}", lambda key: f"{key} ", lambda key: f"\t{key}",
    lambda key: f"\xa0{key}", lambda key: f"{key}x",
)
_ISRCS: tuple[str | None, ...] = (
    None, "GBAAA2600001", "gb-aaa-26-00001", "GB AAA 26 00001", "USBBB2600002",
    "usbbb2600002", "bad", "",
)
_BLANK_HASHES: tuple[str | None, ...] = (None, "", "  ")


def _random_hash(rng: random.Random, present: float) -> str | None:
    """Mostly canonical, sometimes padded or a longer key sharing the prefix."""
    if rng.random() >= present:
        return rng.choice(_BLANK_HASHES)
    key = rng.choice(_KEYS)
    return rng.choice(_ODD_FORMS)(key) if rng.random() < 0.3 else key


def _random_library(conn: sqlite3.Connection, rng: random.Random, rows: int) -> list[str]:
    """Sparse identity keys, so a row often shares one ONLY in an odd stored form.

    Tracks and deletions go through ``StateWriter``, the spoke's own writer;
    persisted remap chains through ``apply_hub_identity_rejects``, which
    records the hub's collapse verdicts a push brings back.
    """
    pks = [f"t{i:03d}" for i in range(rows)]
    with StateWriter(conn, actor="test") as writer:
        for pk in pks:
            writer.upsert_track(
                stable_id=pk, stable_id_tier="fingerprint", title=pk, artists=[],
                album=None, isrc=rng.choice(_ISRCS), duration_ms=None, file_path=None,
                content_hash=_random_hash(rng, 0.6), audio_hash=_random_hash(rng, 0.3),
            )
        for pk in pks:
            if rng.random() < 0.1:
                writer.remove_from_library(pk)
    rejects = [
        protocol.IdentityReject(table="tracks", offered_pk=offered, survivor_pk=survivor)
        for offered, survivor in (rng.sample(pks, 2) for _ in range(rows // 10))
    ]
    apply_hub_identity_rejects(conn, rejects)
    conn.commit()
    return pks


def _elects_the_library(statements: list[str], conn: sqlite3.Connection) -> bool:
    """True once a traced statement read every live track's identity."""
    return sync_set.identity_row_select(conn) in statements


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("mode", ["per-row", "library"])
def test_per_row_verdicts_equal_the_library_wide_election(
    tmp_path: Path, seed: int, mode: str
) -> None:
    """Overshoot control: a verdict that skipped any row sharing a key (a
    padded hash, a separator-formatted ISRC) would pass the growth test and
    hold or release the wrong track. ``library`` first asks about as many
    absent tracks as a walk decides row by row, so the verdicts that follow
    come from the walk's one library election."""
    conn = _open_hub(tmp_path)
    statements: list[str] = []
    try:
        pks = _random_library(conn, random.Random(seed), rows=120)
        expected = set(effective_identity_remap(conn))
        assert expected, "the fixture elected no loser, so it tests nothing"
        order = [*pks, "absent"]
        random.Random(seed).shuffle(order)
        warmup = (
            [f"absent-{i}" for i in range(identity_verdicts.CFG.ELECT_LIBRARY_AFTER_VERDICTS)]
            if mode == "library"
            else []
        )
        held = sync_set.HeldKeys(conn)
        conn.set_trace_callback(statements.append)
        assert not any(held.is_identity_loser(pk) for pk in warmup)
        actual = {pk for pk in order if held.is_identity_loser(pk)}
        conn.set_trace_callback(None)
        elected_library = _elects_the_library(statements, conn)
    finally:
        conn.close()
    assert elected_library == (mode == "library"), (
        f"{mode} walk {'never' if mode == 'library' else 'also'} elected the whole "
        "library, so this case did not test the path it names"
    )
    assert actual == expected


# ----- column memo --------------------------------------------------------------


def _table_info_statements(tmp_path: Path, changes: int) -> int:
    conn = _open_hub(tmp_path / f"apply-{changes}")
    traced: list[str] = []
    try:
        incoming = _incoming(conn)[:changes]
        conn.set_trace_callback(traced.append)
        engine.hub_apply(conn, incoming)
    finally:
        conn.set_trace_callback(None)
        conn.close()
    return sum("table_info" in statement for statement in traced)


def test_an_apply_batch_reads_each_tables_columns_once(tmp_path: Path) -> None:
    few = _table_info_statements(tmp_path, 1)
    many = _table_info_statements(tmp_path, 5)
    assert many == few, (
        f"PRAGMA table_info ran {few} times for 1 row and {many} for 5: the "
        "apply batch re-reads a table's columns per row (LIBM-120 L6)"
    )


def test_the_column_memo_never_answers_for_another_connection(tmp_path: Path) -> None:
    hub = _open_hub(tmp_path)
    other = sqlite3.connect(":memory:")
    try:
        other.execute("CREATE TABLE tracks(stable_id TEXT PRIMARY KEY)")
        with protocol.table_columns_memo(hub):
            assert "content_hash" in protocol.table_columns(hub, "tracks")
            assert protocol.table_columns(other, "tracks") == ("stable_id",)
    finally:
        hub.close()
        other.close()


def test_the_column_memo_ends_with_its_scope() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE tracks(stable_id TEXT PRIMARY KEY)")
        with protocol.table_columns_memo(conn):
            assert protocol.table_columns(conn, "tracks") == ("stable_id",)
        conn.execute("ALTER TABLE tracks ADD COLUMN title TEXT")
        assert protocol.table_columns(conn, "tracks") == ("stable_id", "title")
    finally:
        conn.close()
