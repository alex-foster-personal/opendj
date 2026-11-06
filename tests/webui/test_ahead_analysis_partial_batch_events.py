"""A finished track in a partial ahead-analysis batch still lands (NATIVE-35).

[if] a batch exits 1 [then] analysis.changed publishes for every done id, [else stop].
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

import pytest

from apps.webui.server import ahead_analysis as aa

pytestmark = pytest.mark.requirement("NATIVE-35")

DONE_ID = "done-1"
FAILED_ID = "bad-1"
FAILED_REASON = "DecodeError: bad frame"
LANE = "beatgrid"
BACKEND = "own_beatgrid.backfill"
BATCH_ID = "batch-1"


def _progress(*pairs: tuple[str, str, str | None]) -> list[dict[str, object]]:
    return [{"stable_id": sid, "state": state, "reason": reason} for sid, state, reason in pairs]


def _runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_code: int,
    done: set[str],
    progress_items: list[dict[str, object]],
    yield_fn: object = None,
    xruns_fn: object = None,
    baseline_fn: object = None,
) -> tuple[Callable[[str, str, list[str]], dict[str, str]], list[tuple[str, dict[str, object]]]]:
    events: list[tuple[str, dict[str, object]]] = []

    def queue_cli(args: list[str], db: str, abandon: object = None) -> tuple[int, str, str]:
        verb = args[0]
        if verb == "enqueue":
            return 0, json.dumps({"batch_id": BATCH_ID}), ""
        if verb == "run":
            return run_code, "", "one track failed" if run_code == 1 else f"queue run exit {run_code}"
        if verb == "progress":
            return 0, json.dumps({"items": progress_items}), ""
        if verb == "cancel":
            return 0, "", ""
        raise AssertionError(f"unexpected queue verb {verb}")

    monkeypatch.setattr(aa, "_queue_cli", queue_cli)
    monkeypatch.setattr(aa, "done_ids", lambda _conn_factory, _backend: set(done))
    monkeypatch.setattr(
        aa, "publish_engine_event", lambda topic, payload: events.append((topic, payload))
    )
    run = aa.run_lane_via_queue(
        "unused.db",
        lambda: sqlite3.connect(":memory:"),
        yield_fn=yield_fn,
        xruns_fn=xruns_fn,
        baseline_fn=baseline_fn,
    )
    return run, events


def test_exit_1_partial_batch_publishes_only_for_done_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] one id failed and one finished [then] one event and the failure reason return, [else stop]."""
    run, events = _runner(
        monkeypatch,
        run_code=1,
        done={DONE_ID},
        progress_items=_progress(
            (DONE_ID, "done", None),
            (FAILED_ID, "failed", FAILED_REASON),
        ),
    )
    errors = run(LANE, BACKEND, [DONE_ID, FAILED_ID])
    assert errors == {FAILED_ID: FAILED_REASON}
    assert events == [("analysis.changed", {"stable_id": DONE_ID, "lane": LANE})]


def test_exit_0_batch_publishes_for_every_done_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the queue run exits 0 [then] every done id gets analysis.changed, [else stop]."""
    d2 = "done-2"
    run, events = _runner(
        monkeypatch,
        run_code=0,
        done={DONE_ID, d2},
        progress_items=_progress((DONE_ID, "done", None), (d2, "done", None)),
    )
    ids = [DONE_ID, d2]
    errors = run(LANE, BACKEND, ids)
    assert errors == {}
    assert events == [
        ("analysis.changed", {"stable_id": DONE_ID, "lane": LANE}),
        ("analysis.changed", {"stable_id": d2, "lane": LANE}),
    ]


@pytest.mark.parametrize("run_code", [2, 5, -15])
def test_non_completion_exit_codes_publish_nothing(
    monkeypatch: pytest.MonkeyPatch, run_code: int
) -> None:
    """[if] the run exits neither 0 nor 1 [then] no analysis.changed fires, [else stop]."""
    run, events = _runner(
        monkeypatch,
        run_code=run_code,
        done={DONE_ID},
        progress_items=[],
    )
    errors = run(LANE, BACKEND, [DONE_ID, FAILED_ID])
    assert events == []
    assert DONE_ID not in errors
    fb = errors[FAILED_ID]
    assert fb.startswith(f"queue run exit {run_code}:")


def test_exit_1_with_every_id_done_still_publishes_both(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] exit 1 but every id is current-done [then] two events and no errors, [else stop]."""
    d2 = "done-2"
    run, events = _runner(
        monkeypatch,
        run_code=1,
        done={DONE_ID, d2},
        progress_items=_progress((DONE_ID, "done", None), (d2, "done", None)),
    )
    errors = run(LANE, BACKEND, [DONE_ID, d2])
    assert errors == {}
    assert events == [
        ("analysis.changed", {"stable_id": DONE_ID, "lane": LANE}),
        ("analysis.changed", {"stable_id": d2, "lane": LANE}),
    ]


def test_abandoned_after_enqueue_publishes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] xrun abandons after enqueue [then] the run returns empty and publishes nothing, [else stop]."""
    events: list[tuple[str, dict[str, object]]] = []
    calls: list[str] = []
    count = [1]
    stable_ids = [DONE_ID, FAILED_ID]

    def queue_cli(args: list[str], db: str, abandon: object = None) -> tuple[int, str, str]:
        calls.append(args[0])
        if args[0] == "enqueue":
            count[0] = 2
            return 0, json.dumps({"batch_id": BATCH_ID}), ""
        if args[0] == "cancel":
            return 0, "", ""
        if args[0] == "progress":
            items = _progress(*[(sid, "cancelled", None) for sid in stable_ids])
            return 0, json.dumps({"items": items}), ""
        raise AssertionError(f"unexpected queue verb {args[0]}")

    monkeypatch.setattr(aa, "_queue_cli", queue_cli)
    monkeypatch.setattr(
        aa, "publish_engine_event", lambda topic, payload: events.append((topic, payload))
    )
    run = aa.run_lane_via_queue(
        "unused.db",
        lambda: sqlite3.connect(":memory:"),
        xruns_fn=lambda: count[0],
        baseline_fn=lambda: 1,
    )
    assert run(LANE, BACKEND, stable_ids) == {}
    assert calls == ["enqueue", "cancel", "progress"]
    assert events == []


def test_yielded_before_run_publishes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the runner yields before queue run [then] LaneRunYielded and no events, [else stop]."""
    events: list[tuple[str, dict[str, object]]] = []
    calls: list[str] = []
    stable_ids = [DONE_ID, FAILED_ID]

    def queue_cli(args: list[str], db: str, abandon: object = None) -> tuple[int, str, str]:
        calls.append(args[0])
        if args[0] == "enqueue":
            return 0, json.dumps({"batch_id": BATCH_ID}), ""
        if args[0] == "cancel":
            return 0, "", ""
        if args[0] == "progress":
            items = _progress(*[(sid, "cancelled", None) for sid in stable_ids])
            return 0, json.dumps({"items": items}), ""
        raise AssertionError(f"unexpected queue verb {args[0]}")

    monkeypatch.setattr(aa, "_queue_cli", queue_cli)
    monkeypatch.setattr(
        aa, "publish_engine_event", lambda topic, payload: events.append((topic, payload))
    )
    run = aa.run_lane_via_queue(
        "unused.db", lambda: sqlite3.connect(":memory:"), yield_fn=lambda: True
    )
    with pytest.raises(aa.LaneRunYielded):
        run(LANE, BACKEND, stable_ids)
    assert "run" not in calls
    assert calls == ["enqueue", "cancel", "progress"]
    assert events == []
