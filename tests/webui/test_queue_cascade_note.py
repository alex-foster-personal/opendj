"""A dependency-cascade batch names the batch it was cascaded from.

- [if] a run's cascade enqueues a follow-up batch [then] that batch can be found from its parent, [else stop].

Single-line intent, one assertion block each:
- if the follow-up batch does not name its parent, a drain that dies before
  running it cannot settle it, and it sits queued with no runner -- broken.
- if a lookup by one parent also returns another parent's cascade (an id
  that merely starts the same way, or a non-cascade note that mentions it),
  a dead drain fails work that was never its own -- broken.
- if the queue CLI accepts an operator's note that reads as the engine's own
  (a drained batch's mark, a cascade's prefix), the next start settles that
  operator's batch -- broken; if it refuses an ordinary note that merely
  mentions either (the overshoot) -- broken.

-Claude
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.analysis import admission, queue_cli, queue_store
from apps.analysis import queue as queue_api
from apps.analysis.queue import CascadeOutcome
from apps.analysis.queue_cascade_note import (
    cascade_children,
    cascade_note,
    cascade_parent,
    reserved_note_reason,
)
from apps.analysis.queue_runner import _enqueue_cascade
from apps.analysis.store import open_conn
from tests.webui import drain_media

pytestmark = pytest.mark.requirement("NATIVE-5054")


def _key_candidates(tmp_path: Path, stable_ids: list[str]) -> list[admission.Candidate]:
    out = []
    for sid in stable_ids:
        audio = tmp_path / f"{sid}.wav"
        audio.write_bytes(b"\0")
        out.append(
            admission.Candidate(
                stable_id=sid,
                lane="key",
                backend="own_key.backfill",
                file_path=str(audio),
                duration_s=180.0,
            )
        )
    return out


def test_a_cascade_batch_is_found_from_the_batch_that_cascaded_it(tmp_path: Path) -> None:
    conn = open_conn(tmp_path / "state.db")
    try:
        queue_store.ensure_queue_tables(conn)
        outcomes = [CascadeOutcome(stable_id="t1", lane="key", requeued=True, reason="grid moved")]

        def resolver(_conn: object, stable_ids: list[str], *, lane: str) -> list:
            assert lane == "key"
            return _key_candidates(tmp_path, stable_ids)

        child = _enqueue_cascade(conn, outcomes, resolver, parent_batch_id="qb_parent")
        assert child is not None
        batch = queue_store.get_batch(conn, child)
        assert batch is not None
        assert batch.note == cascade_note("key", "qb_parent")
        assert cascade_children(conn, "qb_parent") == [child]
    finally:
        conn.close()


def test_a_lookup_returns_only_that_parents_cascades(tmp_path: Path) -> None:
    """Controls: a prefix of the parent id, and a plain note naming the parent."""
    conn = open_conn(tmp_path / "state.db")
    try:
        own = queue_api.enqueue(conn, _key_candidates(tmp_path, ["a"]), note=cascade_note("key", "qb_parent")).batch_id
        queue_api.enqueue(conn, _key_candidates(tmp_path, ["b"]), note=cascade_note("key", "qb_parent2"))
        queue_api.enqueue(conn, _key_candidates(tmp_path, ["c"]), note="operator note (from batch qb_parent)")
        assert cascade_children(conn, "qb_parent") == [own]
        assert cascade_children(conn, "qb_paren") == []
    finally:
        conn.close()


def test_cascade_parent_reads_the_parent_back_from_a_cascade_note_only() -> None:
    assert cascade_parent(cascade_note("key", "qb_parent")) == "qb_parent"
    # Controls: a note that only mentions a batch, or no note, names no parent.
    assert cascade_parent("rerun (from batch qb_parent)") is None
    assert cascade_parent("track context menu") is None
    assert cascade_parent(None) is None


def _cli_enqueue(db: Path, stable_id: str, note: str) -> int:
    return queue_cli.main(
        [
            "--db",
            str(db),
            "--json",
            "enqueue",
            "--lane",
            drain_media.LANE,
            "--backend",
            drain_media.BACKEND,
            "--stable-id",
            stable_id,
            "--note",
            note,
        ]
    )


def _notes(db: Path) -> list[str | None]:
    conn = open_conn(db)
    try:
        queue_store.ensure_queue_tables(conn)
        return [row[0] for row in conn.execute("SELECT note FROM analysis_queue_batch")]
    finally:
        conn.close()


def test_the_cli_refuses_a_note_that_reads_as_the_engines_and_keeps_an_ordinary_one(
    tmp_path: Path,
) -> None:
    """Codex P2 on PR #5054, on the operator's other enqueue path: an imported track,
    enqueued through the CLI's own entry point."""
    stable_id = drain_media.import_tracks(tmp_path, ["track"])["track"]
    db = drain_media.state_db(tmp_path)
    for note in (
        "by hand (run by the engine)",
        cascade_note("key", "qb_1"),
    ):
        assert reserved_note_reason(note) is not None
        assert _cli_enqueue(db, stable_id, note) == queue_cli.EXIT_USAGE, note
    assert _notes(db) == [], "a refused note still made a batch"
    ordinary = [
        "ahead-analysis",
        "(run by the engine) asked for by hand",
        "after a dependency cascade: key",
    ]
    for note in ordinary:
        assert reserved_note_reason(note) is None
        assert _cli_enqueue(db, stable_id, note) == 0, note
    assert sorted(_notes(db)) == sorted(ordinary)
