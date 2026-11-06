"""probe_all answers within its deadline when a listing blocks (setup hang, Fri 9 Oct 2026).

The unsigned Electron build on Silver showed "The app took too long to answer"
because /api/v1/setup/status and /setup/permissions never returned. Both run
``fs_access.probe_all`` over ~/Music, ~/Desktop and ~/Documents, and on macOS a
listing of those folders blocks until the person answers the consent dialog.
These tests block one listing on purpose and prove the call still answers.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.shared import fs_access
from tests.waits import THREAD_HANG_GUARD_S


def _give_up_after(*events: threading.Event) -> threading.Event:
    give_up = threading.Event()

    def _arm() -> None:
        try:
            for event in events:
                event.wait(THREAD_HANG_GUARD_S)
        finally:
            give_up.set()

    threading.Thread(target=_arm, daemon=True, name="fs-access-test-give-up").start()
    return give_up


@pytest.fixture
def blocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict]:
    """Block the listing of one directory until the test releases it."""
    stuck = tmp_path / "Desktop"
    stuck.mkdir()
    fine = tmp_path / "Music"
    fine.mkdir()
    (fine / "a.mp3").write_bytes(b"x")
    release = threading.Event()
    calls: list[Path] = []
    finished: dict[Path, threading.Event] = {}
    entered: dict[Path, threading.Event] = {}
    finished_lock = threading.Lock()
    real = fs_access.probe_readable

    def _done_for(path: Path) -> threading.Event:
        with finished_lock:
            return finished.setdefault(path, threading.Event())

    def _entered_for(path: Path) -> threading.Event:
        with finished_lock:
            return entered.setdefault(path, threading.Event())

    def _probe(path: Path) -> fs_access.AccessProbe:
        calls.append(path)
        _entered_for(path).set()
        try:
            if path == stuck:
                release.wait(30)
            return real(path)
        finally:
            _done_for(path).set()

    monkeypatch.setattr(fs_access, "probe_readable", _probe)
    yield {
        "stuck": stuck,
        "fine": fine,
        "release": release,
        "calls": calls,
        "done_for": _done_for,
        "entered_for": _entered_for,
    }
    release.set()
    with fs_access._IN_FLIGHT_LOCK:
        fs_access._IN_FLIGHT.clear()


def test_a_blocked_listing_answers_unknown_within_the_deadline(blocked: dict) -> None:
    started = time.monotonic()
    probes = fs_access.probe_all(
        [blocked["fine"], blocked["stuck"]],
        deadline_s=0.3,
        # Wait for the stuck listing to have started too: under load its thread can
        # miss the 0.3 s deadline before it runs, and that reads "not checked".
        give_up=_give_up_after(blocked["done_for"](blocked["fine"]), blocked["entered_for"](blocked["stuck"])),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"probe_all took {elapsed:.2f} s with a 0.3 s deadline"
    fine, stuck = probes
    assert fine.readable is True
    assert stuck.path == str(blocked["stuck"])
    assert stuck.readable is False
    assert stuck.denied is False, "a listing that never answered is not proof of a denial"
    assert "no answer within 0.3 s" in stuck.detail
    assert fs_access.denied_roots(probes) == []


def test_the_next_call_reuses_the_stuck_listing_and_reads_its_answer(blocked: dict) -> None:
    fs_access.probe_all(
        [blocked["stuck"]],
        deadline_s=0.2,
        give_up=_give_up_after(blocked["entered_for"](blocked["stuck"])),
    )
    fs_access.probe_all(
        [blocked["stuck"]],
        deadline_s=0.2,
        give_up=_give_up_after(blocked["entered_for"](blocked["stuck"])),
    )
    assert blocked["calls"].count(blocked["stuck"]) == 1, "a second thread was started for a path already stuck"

    with fs_access._IN_FLIGHT_LOCK:
        job = fs_access._IN_FLIGHT[blocked["stuck"]]
    blocked["release"].set()
    job.thread.join(5)
    assert not job.thread.is_alive()

    (again,) = fs_access.probe_all([blocked["stuck"]], deadline_s=None)
    assert again.readable is True
    assert again.detail == "readable"
    assert blocked["calls"].count(blocked["stuck"]) == 2, (
        "a finished listing was not cleared, so the path was never re-probed"
    )


def test_unblocked_listings_keep_their_real_answers(tmp_path: Path) -> None:
    """Control: with nothing blocking, every state still reads as before."""
    present = tmp_path / "present"
    present.mkdir()
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    missing = tmp_path / "missing"

    probes = fs_access.probe_all([present, a_file, missing, present])

    assert [p.path for p in probes] == [str(present), str(a_file), str(missing)]
    assert probes[0].readable is True
    assert probes[1].detail == "exists but is not a directory"
    assert probes[2].exists is False


def test_an_import_waits_for_a_root_still_on_its_consent_dialog(blocked: dict) -> None:
    """An import walk must not drop a root that has not answered yet (Sentry on #6825).

    A root with no answer reads neither readable nor denied, so a deadline in
    the import path would leave it out of the walk and report a smaller
    library with no error. The listing here answers after the setup deadline.
    """
    from apps.shared.state.ingest import folder as folder_ingest

    (blocked["stuck"] / "b.mp3").write_bytes(b"x")
    threading.Timer(fs_access.PROBE_DEADLINE_S + 0.5, blocked["release"].set).start()

    files, denied, _dataless = folder_ingest.collect_audio([blocked["fine"], blocked["stuck"]])

    assert sorted(entry.path.name for entry in files) == ["a.mp3", "b.mp3"]
    assert denied == []


def test_a_root_with_no_answer_is_not_counted_readable(blocked: dict, tmp_path: Path) -> None:
    """The setup summary must not read all_readable for a root still on its dialog (Codex on #6825)."""
    missing = tmp_path / "Documents"
    probes = fs_access.probe_all(
        [blocked["fine"], blocked["stuck"], missing],
        deadline_s=0.2,
        give_up=_give_up_after(blocked["done_for"](blocked["fine"]), blocked["done_for"](missing)),
    )

    assert fs_access.denied_roots(probes) == []
    assert fs_access.unreadable_roots(probes) == [str(blocked["stuck"])]
    # Control: once the listing answers, nothing present is unreadable.
    blocked["release"].set()
    answered = fs_access.probe_all([blocked["fine"], blocked["stuck"], missing], deadline_s=None)
    assert fs_access.unreadable_roots(answered) == []


def test_a_no_answer_probe_stays_pending_after_its_listing_finishes(blocked: dict) -> None:
    """The import preflight reads pending from the probe, not the thread (Codex P1 on #6825).

    A listing that answers between probe_all returning and the preflight check
    must not turn a no-answer probe into a 409 refusal.
    """
    probes = fs_access.probe_all(
        [blocked["fine"], blocked["stuck"]],
        deadline_s=0.2,
        # Wait for the stuck listing to have started too: under load its thread can
        # miss registration before give_up fires, and _IN_FLIGHT[stuck] KeyErrors.
        give_up=_give_up_after(
            blocked["done_for"](blocked["fine"]),
            blocked["entered_for"](blocked["stuck"]),
        ),
    )
    with fs_access._IN_FLIGHT_LOCK:
        job = fs_access._IN_FLIGHT[blocked["stuck"]]
    blocked["release"].set()
    job.thread.join(5)
    assert not job.thread.is_alive()

    assert fs_access.pending_roots(probes) == [str(blocked["stuck"])]
    # Control: the answered root and a fresh, answered probe are not pending.
    answered = fs_access.probe_all([blocked["fine"], blocked["stuck"]], deadline_s=None)
    assert fs_access.pending_roots(answered) == []
    assert [probe.pending for probe in probes] == [False, True]


def test_blocked_listings_are_capped_across_callers(blocked: dict, tmp_path: Path) -> None:
    """A path past the thread ceiling answers no-answer without a thread (Codex P1 on #6825)."""
    other = tmp_path / "Documents"
    other.mkdir()
    first = fs_access.probe_all(
        [blocked["stuck"]],
        deadline_s=0.2,
        max_threads=1,
        give_up=_give_up_after(blocked["entered_for"](blocked["stuck"])),
    )
    assert [probe.pending for probe in first] == [True]

    capped = fs_access.probe_all([other], deadline_s=0.2, max_threads=1)
    assert [probe.pending for probe in capped] == [True]
    assert other not in blocked["calls"], "a listing started past the thread ceiling"
    # Never examined, so never claimed present (Sol P1 on #6825), yet still not readable.
    assert [probe.exists for probe in capped] == [False]
    assert fs_access.unreadable_roots(capped) == [str(other)]
    # Control: with room under the ceiling the same path is listed and answers.
    roomy = fs_access.probe_all([other], deadline_s=None, max_threads=2)
    assert [probe.readable for probe in roomy] == [True]
    # An import (no deadline) is never capped: it needs every answer.
    blocked["release"].set()
    waited = fs_access.probe_all([blocked["stuck"], other], deadline_s=None, max_threads=0)
    assert [probe.readable for probe in waited] == [True, True]


def test_a_timed_out_listing_reports_the_existence_it_established(blocked: dict, tmp_path: Path) -> None:
    """exists is true only when checked: the blocked listing got past its existence check."""
    (stuck,) = fs_access.probe_all(
        [blocked["stuck"]],
        deadline_s=0.2,
        give_up=_give_up_after(blocked["entered_for"](blocked["stuck"])),
    )
    assert stuck.pending is True
    assert stuck.exists is True


def test_the_deadline_covers_waiting_for_the_lock(tmp_path: Path) -> None:
    """The clock starts at entry, not after scheduling (Sol P1 on #6825)."""
    import threading as _threading

    folder = tmp_path / "Music"
    folder.mkdir()
    held = _threading.Event()
    done = _threading.Event()

    def _hold() -> None:
        with fs_access._IN_FLIGHT_LOCK:
            held.set()
            done.wait(5)

    holder = _threading.Thread(target=_hold)
    holder.start()
    held.wait(5)
    give_up = _threading.Event()
    give_up.set()
    try:
        started = time.monotonic()
        (probe,) = fs_access.probe_all([folder], deadline_s=0.3, give_up=give_up)
        elapsed = time.monotonic() - started
    finally:
        done.set()
        holder.join(5)
    assert elapsed < 1.0, f"probe_all took {elapsed:.2f} s with a 0.3 s deadline behind a held lock"
    assert probe.pending is True
    assert probe.exists is False
    # Control: with the lock free the same folder answers.
    (answered,) = fs_access.probe_all([folder], deadline_s=None)
    assert answered.readable is True


def test_a_late_listing_leaves_the_map_without_being_asked_again(blocked: dict) -> None:
    """Finished listings remove themselves, so the map does not grow with distinct slow paths."""
    fs_access.probe_all(
        [blocked["stuck"]],
        deadline_s=0.2,
        give_up=_give_up_after(blocked["entered_for"](blocked["stuck"])),
    )
    with fs_access._IN_FLIGHT_LOCK:
        job = fs_access._IN_FLIGHT[blocked["stuck"]]
    blocked["release"].set()
    job.thread.join(5)
    with fs_access._IN_FLIGHT_LOCK:
        assert blocked["stuck"] not in fs_access._IN_FLIGHT
