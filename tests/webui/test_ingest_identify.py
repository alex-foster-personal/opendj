"""Fingerprint identify lane orchestration (issue #10429).

Regression one-liners:
  - [if] row has artist [then] has_artist, no lookup, [else stop].
  - [if] key missing [then] unavailable without identify_fn, [else stop].
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.tags.identify import Candidate, IdentifyDeferred, IdentifyError, IdentifyProposal
from apps.webui.server.ingest_identify import IdentifyOutcome, TrackRow, run_identify

pytestmark = pytest.mark.requirement("SETPREP-12099")


def _row(*, artist: str = "", title: str = "file") -> TrackRow:
    return TrackRow("t1", title, artist, [], "/music/track.mp3")


def test_run_identify_skips_rows_with_artist() -> None:
    recorded: list[str] = []

    def identify_fn(fp: str, duration: float) -> IdentifyProposal | None:
        recorded.append(fp)
        return None

    out = run_identify(
        ["t1"],
        rows_fn=lambda _ids: {"t1": _row(artist="Already")},
        fingerprint_fn=lambda _p: ("fp", 1.0),
        identify_fn=identify_fn,
        record_fn=lambda _items: None,
        key_fn=lambda: "key",
    )
    assert out["t1"].state == "has_artist"
    assert recorded == []


def test_run_identify_no_key_unavailable() -> None:
    calls: list[str] = []

    def identify_fn(fp: str, duration: float) -> IdentifyProposal | None:
        calls.append(fp)
        return None

    out = run_identify(
        ["t1"],
        rows_fn=lambda _ids: {"t1": _row()},
        fingerprint_fn=lambda _p: ("fp", 1.0),
        identify_fn=identify_fn,
        record_fn=lambda _items: None,
        key_fn=lambda: None,
    )
    assert out["t1"].state == "unavailable"
    assert calls == []


def test_run_identify_fingerprint_failure_failed() -> None:
    class Boom(Exception):
        pass

    out = run_identify(
        ["t1"],
        rows_fn=lambda _ids: {"t1": _row()},
        fingerprint_fn=lambda _p: (_ for _ in ()).throw(Boom("nope")),
        identify_fn=lambda _f, _d: None,
        record_fn=lambda _items: None,
        key_fn=lambda: "key",
    )
    assert out["t1"].state == "failed"
    assert "Boom" in (out["t1"].reason or "")


def test_run_identify_deferred_rest_of_batch() -> None:
    def identify_fn(fp: str, duration: float) -> IdentifyProposal | None:
        if fp == "first":
            raise IdentifyDeferred("busy")
        return None

    out = run_identify(
        ["a", "b"],
        rows_fn=lambda ids: {sid: TrackRow(sid, sid, "", [], f"/music/{sid}.mp3") for sid in ids},
        fingerprint_fn=lambda path: ("first" if path.endswith("a.mp3") else "second", 1.0),
        identify_fn=identify_fn,
        record_fn=lambda _items: None,
        key_fn=lambda: "key",
    )
    assert out["a"].state == "deferred"
    assert out["b"].state == "deferred"


def test_run_identify_record_fn_called_once() -> None:
    recorded: list[Any] = []
    proposal = IdentifyProposal(
        status="confident",
        score=0.95,
        best=Candidate("mbid", "Artist", "Title", 2000, "Label"),
        candidates=(Candidate("mbid", "Artist", "Title", 2000, "Label"),),
    )

    run_identify(
        ["t1"],
        rows_fn=lambda _ids: {"t1": _row()},
        fingerprint_fn=lambda _p: ("fp", 1.0),
        identify_fn=lambda _f, _d: proposal,
        record_fn=lambda items: recorded.append(list(items)),
        key_fn=lambda: "key",
    )
    assert len(recorded) == 1
    assert len(recorded[0]) == 1


def test_run_identify_identify_error_maps_failed() -> None:
    out = run_identify(
        ["t1"],
        rows_fn=lambda _ids: {"t1": _row()},
        fingerprint_fn=lambda _p: ("fp", 1.0),
        identify_fn=lambda _f, _d: (_ for _ in ()).throw(IdentifyError("bad payload")),
        record_fn=lambda _items: None,
        key_fn=lambda: "key",
    )
    assert out["t1"].state == "failed"
    assert out["t1"].reason == "bad payload"


def test_worker_survives_rows_fn_failure() -> None:
    from apps.webui.server.ingest_identify import IdentifyWorker

    seen: list[dict[str, IdentifyOutcome]] = []

    def boom(_ids: list[str]) -> dict[str, IdentifyOutcome]:
        raise RuntimeError("rows_fn broke")

    worker = IdentifyWorker(boom, on_outcomes=lambda _bid, outcomes: seen.append(dict(outcomes)), maxsize=10)
    assert worker.schedule("batch", ["x", "y"]) == 2
    worker.join()
    assert seen == [
        {
            "x": IdentifyOutcome("failed", "identify worker error"),
            "y": IdentifyOutcome("failed", "identify worker error"),
        }
    ]
