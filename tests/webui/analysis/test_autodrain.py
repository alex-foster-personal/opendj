"""The analyze-on-import reconcile loop: when the daemon drains by itself.

Split out of tests/webui/test_analysis_queue.py, which owns the route and
queue-read surface. This module owns the watcher: what one tick decides and
how the daemon arms the loop. Which attempts a tick may ADOPT - the two
windows where the one-slot registry holds a job whose verdict is not in yet -
lives next door in test_autodrain_booking.py.

Every case drives the watcher the PRODUCTION builder returns
(``app_mod.build_auto_analyze_watcher``). Re-declaring its four callables here
would keep the file green after the daemon started draining the wrong scope.

Regression lines:
  - if the auto-drain re-fires on an unchanged failed queue then broken
  - if a refused start still consumes the attempt then broken
  - if the attempt is booked against the watcher's scan rather than the queue
    the worker actually read then broken
  - if a queue that empties and returns byte-identical is never drained then broken
  - if a newly imported track does not re-arm the auto-drain then broken
  - if create_app arms the auto-drain by default then broken
  - if the daemon lifespan does not run and join a real reconcile thread then broken
  - if an inherited MUSIC_DJ_AUTO_ANALYZE=on survives into the suite then broken
  - if the loop is armed on a machine with no analyzer installed then broken
  - if that refusal is silent rather than logged then broken
"""
from __future__ import annotations

import inspect
import os
import re
import sqlite3
import subprocess
import sys

import pytest
from fastapi import HTTPException

from apps.shared.paths import PROJECT_ROOT
from apps.webui.server import analysis_autostart
from apps.webui.server.routes import ingest as ingest_mod
from tests.webui.analysis.conftest import (
    _audio,
    _enable_all_steps,
    _seed,
    _wait,
    _watcher,
)


def test_auto_drain_is_inert_when_disabled(app, tmp_path):
    _seed(app, "junk0001", _audio(tmp_path, "junk.mp3"))
    assert _watcher(app, enabled=False).tick() == "disabled"
    assert ingest_mod.refresh_status().phase == "idle"


def test_auto_drain_does_nothing_on_an_empty_backlog(app, client):
    _enable_all_steps(client)
    assert _watcher(app).tick() == "empty"


@pytest.mark.requirement("PARITY-06")
def test_auto_drain_starts_on_an_import_and_does_not_retry_the_same_queue(
    app, client, tmp_path
):
    _seed(app, "junk0001", _audio(tmp_path, "junk.mp3"))
    _enable_all_steps(client)
    watcher = _watcher(app)

    assert watcher.tick() == "started"
    assert app.state.auto_analyze.attempts == 1
    assert app.state.auto_analyze.last_signature is None, (
        "nothing is booked until the worker has read a queue: what it reads "
        "is not knowable at start time"
    )

    status = _wait(client)
    assert status["phase"] == "error", "fixture precondition: the drain failed"

    # Same failed queue: re-firing every tick would be a retry storm.
    assert watcher.tick() == "unchanged"
    assert app.state.auto_analyze.attempts == 1
    signature = app.state.auto_analyze.last_signature
    assert signature, "the failed queue was never booked, so nothing suppresses"

    # A NEW import changes the queue, so the drain is armed again.
    _seed(app, "junk0002", _audio(tmp_path, "junk2.mp3"))
    assert watcher.tick() == "started"
    _wait(client)
    assert ingest_mod._JOBS.current.queue_signature != signature, (
        "the second drain read the same queue as the first"
    )
    assert watcher.tick() == "unchanged"
    assert app.state.auto_analyze.last_signature != signature


@pytest.mark.requirement("PARITY-06")
def test_a_refused_start_does_not_consume_the_attempt(app, client, tmp_path):
    """The drain can refuse to start for real reasons: the analysis step is
    off, or a manual refresh claimed the one job slot after the busy check.
    Booking the attempt before the drain is actually running would make every
    later tick report "unchanged", and the backlog would never drain until
    some unrelated import moved the signature."""
    _seed(app, "junk0001", _audio(tmp_path, "junk.mp3"))
    client.put("/api/v1/ingest/config",
               json={"enabled": {"analysis": False, "stems": True, "vocals": True}})
    watcher = _watcher(app)

    with pytest.raises(HTTPException) as refusal:
        watcher.tick()
    assert refusal.value.status_code == 422
    assert app.state.auto_analyze.attempts == 0
    assert app.state.auto_analyze.last_signature is None

    # The refusal is over, so the same unchanged queue must still be drained.
    _enable_all_steps(client)
    assert watcher.tick() == "started"
    assert app.state.auto_analyze.attempts == 1
    _wait(client)


@pytest.mark.requirement("PARITY-06")
def _map_to_vendor(app, sid, *, vendor="rekordbox"):
    """Give a track a live vendor mapping, which retires it from the backlog.

    Written straight to the production table the backlog query reads, so the
    track leaves the pending set exactly as a real rekordbox import would
    retire it - and without touching the audio file, whose stat fields are
    what the returning queue has to reproduce.
    """
    conn = sqlite3.connect(app.state.state_db)
    conn.execute(
        "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES (?,?,?)",
        (sid, vendor, f"vid-{sid}"),
    )
    conn.commit()
    conn.close()


def _unmap_from_vendor(app, sid):
    """Withdraw that mapping, returning the track to the unmapped backlog."""
    conn = sqlite3.connect(app.state.state_db)
    conn.execute("DELETE FROM track_vendor_ids WHERE stable_id = ?", (sid,))
    conn.commit()
    conn.close()


def test_a_queue_that_empties_and_comes_back_identical_is_drained_again(
    app, client, tmp_path
):
    """The watcher scans, then the drain's worker rescans inside its own job.

    A track that leaves the pending set between those two scans leaves the
    worker nothing to do, so the job finishes CLEAN having analyzed nothing. A
    watcher that still remembered that queue's signature as "already
    attempted" would report "unchanged" forever once the track came back and
    would never analyze it, with no unrelated import to rescue it.

    An EMPTY queue has no failed attempt left to suppress, so observing one
    forgets the signature. That is what makes the loop recover here without
    weakening the retry-storm guard: a queue that is still non-empty and still
    unchanged is still refused below.

    The track leaves by acquiring a vendor mapping and returns when that
    mapping is removed, which is the real shape of this race (a rekordbox
    import lands mid-drain, then the mapping is withdrawn) and, crucially,
    never touches the file. That is what makes the returning queue genuinely
    byte-identical, which is the state this test exists to exercise. Moving
    the FILE away and back would no longer produce it: ``_content_token``
    carries ``ctime_ns``, and a rename bumps ctime, so the restored queue
    would differ and the recovery would be proved by the wrong mechanism.
    """
    path = _audio(tmp_path, "away.mp3")
    _seed(app, "away0001", path)
    _enable_all_steps(client)
    watcher = _watcher(app)

    assert watcher.tick() == "started"
    _wait(client)
    signature = ingest_mod._JOBS.current.queue_signature
    assert signature, "fixture precondition: the drain read a non-empty queue"

    _map_to_vendor(app, "away0001")
    assert watcher.tick() == "empty"

    _unmap_from_vendor(app, "away0001")
    assert watcher.tick() == "started", (
        "the same-signature queue came back and was never analyzed"
    )
    _wait(client)
    assert ingest_mod._JOBS.current.queue_signature == signature, (
        "fixture precondition: the restored queue must be byte-identical, "
        "or this test is not exercising the stall it exists for"
    )


@pytest.mark.requirement("PARITY-06")
def test_a_failed_manual_drain_suppresses_the_next_watcher_tick(
    app, client, tmp_path
):
    """A drain the watcher did not start still attempted the queue.

    The daemon exposes POST /analysis-queue/run, which drains exactly the
    derived queue the loop watches. Right after startup last_signature is
    None, so if the watcher only books drains IT started, the first tick
    after a manual drain sees a non-empty queue that matches nothing and
    relaunches the identical queue the user just watched fail.

    Nothing is stubbed: the manual drain is the production route, it really
    shells out to apps.analysis.run, and the file it is handed really is
    undecodable, so the failure is real and the queue really is unchanged
    afterwards.
    """
    _seed(app, "manu0001", _audio(tmp_path, "manual.mp3"))
    _enable_all_steps(client)
    watcher = _watcher(app)

    assert client.post("/api/v1/analysis-queue/run").status_code == 202
    status = _wait(client)
    assert status["phase"] == "error", status["log_tail"]
    attempted = ingest_mod._JOBS.current.queue_signature
    assert attempted, "fixture precondition: the manual drain read a real queue"

    assert watcher.tick() == "unchanged", (
        "the watcher relaunched a queue a manual drain had just failed on"
    )
    assert app.state.auto_analyze.last_signature == attempted
    assert app.state.auto_analyze.attempts == 0, (
        "booking someone else's attempt must not count as one of ours"
    )


@pytest.mark.requirement("PARITY-06")
def test_a_library_refresh_never_retires_the_unmapped_queue(app, client, tmp_path):
    """The converse guard: only unmapped drains may book an unmapped queue.

    A library-scope refresh targets a different set and publishes no queue
    signature. Booking it would retire a backlog it never looked at, so the
    tick after one must still fire.

    And it must not ERASE one either. A cleared signature from an unmapped
    drain is a statement the watcher acts on; the identical absence from a
    library job means only that the job was never about this queue. The second
    half below pins that difference: a library refresh arriving AFTER a real
    booking leaves it exactly where it was.
    """
    _seed(app, "libr0001", _audio(tmp_path, "library.mp3"))
    _enable_all_steps(client)
    watcher = _watcher(app)

    assert client.post(
        "/api/v1/ingest/refresh", json={"scope": "library"}
    ).status_code == 202
    _wait(client)
    assert ingest_mod._JOBS.current.queue_signature is None, (
        "fixture precondition: a library job must publish no queue signature"
    )

    assert watcher.tick() == "started", (
        "a library refresh retired an unmapped queue it never targeted"
    )
    assert app.state.auto_analyze.last_signature is None

    _wait(client)
    assert watcher.tick() == "unchanged"
    booked = app.state.auto_analyze.last_signature
    assert booked, "fixture precondition: a real booking to protect"

    assert client.post(
        "/api/v1/ingest/refresh", json={"scope": "library"}
    ).status_code == 202
    _wait(client)

    assert watcher.tick() == "unchanged", (
        "a library refresh erased the booking made by a real unmapped drain, "
        "so the identical failed queue is relaunched on the next tick"
    )
    assert app.state.auto_analyze.last_signature == booked


@pytest.mark.requirement("PARITY-06")
def test_the_attempt_is_booked_against_the_queue_the_worker_consumed(
    app, client, tmp_path
):
    """The watcher scans, then the worker rescans inside its own job.

    Those are two reads of a derived queue and they can differ. Booking the
    attempt against the watcher's snapshot retires whatever the WORKER
    happened not to consume, and a file restored by rename reproduces size
    and mtime_ns exactly - so that snapshot would match forever, with no
    polling tick obliged to observe the gap. The drain therefore publishes
    what it read, and the watcher books against that.
    """
    _seed(app, "junk0001", _audio(tmp_path, "junk.mp3"))
    _enable_all_steps(client)
    watcher = _watcher(app)

    assert watcher.tick() == "started"
    _wait(client)
    watcher_snapshot = ingest_mod._JOBS.current.queue_signature
    assert watcher_snapshot, "the unmapped drain did not publish the queue it read"

    # A second import, then a MANUAL drain over the bigger queue. That job
    # reads {junk0001, junk0002} - a queue the watcher never scanned - and
    # publishes it, so the two snapshots now genuinely differ.
    _seed(app, "junk0002", _audio(tmp_path, "junk2.mp3"))
    assert client.post("/api/v1/ingest/refresh",
                       json={"scope": "unmapped"}).status_code == 202
    _wait(client)
    consumed = ingest_mod._JOBS.current.queue_signature
    assert consumed and consumed != watcher_snapshot, (
        "fixture precondition: the manual drain must have read a different "
        "queue, or this test is not discriminating"
    )

    # Booking against the watcher's own stale snapshot would launch a third
    # drain over a queue that was just read and failed.
    assert watcher.tick() == "unchanged"
    assert app.state.auto_analyze.last_signature == consumed, (
        "the attempt was booked against the watcher's own scan, not the "
        "snapshot the worker actually consumed"
    )


def test_auto_drain_yields_to_a_running_job(app, client, tmp_path):
    _seed(app, "junk0001", _audio(tmp_path, "junk.mp3"))
    _enable_all_steps(client)
    assert client.post("/api/v1/ingest/refresh").status_code == 202
    assert _watcher(app).tick() == "busy"
    _wait(client)


@pytest.mark.requirement("PARITY-06")
def test_auto_analyze_env_defaults_on_and_rejects_junk():
    assert analysis_autostart.enabled_from_environ({}) is True
    assert analysis_autostart.enabled_from_environ({"MUSIC_DJ_AUTO_ANALYZE": "off"}) is False
    assert analysis_autostart.enabled_from_environ({"MUSIC_DJ_AUTO_ANALYZE": "on"}) is True
    with pytest.raises(ValueError, match="MUSIC_DJ_AUTO_ANALYZE"):
        analysis_autostart.enabled_from_environ({"MUSIC_DJ_AUTO_ANALYZE": "true"})

@pytest.mark.requirement("PARITY-06")
def test_the_suite_forces_the_loop_off_over_a_shell_that_asked_for_it():
    """The suite's guarantee is "off", not "off unless you exported on".

    A developer who runs the daemon in the same shell exports
    ``MUSIC_DJ_AUTO_ANALYZE=on``, and pytest inherits it. Engine tests build
    the production app through ``_compose_legacy``, which reads that value,
    so a conftest that merely SUPPLIED a default would let a TestClient
    lifespan arm the reconcile loop and spawn real analyzer subprocesses
    against whatever state DB the test had pointed at.

    Proven the only way it can be: a child interpreter that really inherits
    ``on`` imports the real conftest and reports what the variable holds
    afterwards. Nothing is patched, and asserting on this process would
    prove nothing - it never had ``on`` to override.
    """
    child = subprocess.run(
        [sys.executable, "-c",
         "import conftest, os; print(os.environ['MUSIC_DJ_AUTO_ANALYZE'])"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "MUSIC_DJ_AUTO_ANALYZE": "on"},
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "off", (
        "the suite honored an inherited MUSIC_DJ_AUTO_ANALYZE=on, so a "
        f"TestClient lifespan can start real analyzer subprocesses: {child.stdout!r}"
    )


# ----- arming the loop needs an analyzer, not just an env var ----------------

@pytest.mark.requirement("PARITY-06")
def test_arming_needs_the_analyzer_the_drain_would_actually_run():
    """``MUSIC_DJ_AUTO_ANALYZE=on`` is a request, not a capability.

    The packaged Open DJ engine installs the ``uv export --no-dev`` closure
    (``scripts/build_engine_payload.py::locked_requirements``), and librosa and
    scipy live in the optional ``analysis`` extra, so the installed desktop app
    has no analyzer. Arming the reconcile loop there gives every first import a
    failed job and nothing else: the drain raises ``BackendNotAvailable``, the
    queue stays pending, and the signature guard then suppresses the retries.

    The probe must ask the SAME question ``LibrosaBackend._require_deps``
    raises on, or the two drift and the daemon arms a loop the runner refuses.
    """
    from apps.analysis.backends import (
        DEFAULT_BACKEND_MODULES,
        default_backend_installed,
    )
    from apps.analysis.backends import librosa as librosa_backend

    required = set(re.findall(r"^\s+import (\w+)", inspect.getsource(
        librosa_backend.LibrosaBackend._require_deps
    ), re.MULTILINE))
    assert set(DEFAULT_BACKEND_MODULES) == required, (
        "the availability probe and _require_deps disagree about what the "
        f"default backend needs: {sorted(DEFAULT_BACKEND_MODULES)} vs "
        f"{sorted(required)}"
    )

    assert analysis_autostart.arm_from_environ({"MUSIC_DJ_AUTO_ANALYZE": "off"}) is False
    assert analysis_autostart.arm_from_environ(
        {"MUSIC_DJ_AUTO_ANALYZE": "on"}
    ) is default_backend_installed()


@pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("librosa") is not None,
    reason="the analysis extra IS installed here, so the declined-arm branch "
           "cannot be observed; the assertion above still pins the composition",
)
@pytest.mark.requirement("PARITY-06")
def test_the_loop_is_not_armed_without_the_analysis_extra(caplog):
    """Declined LOUDLY. A silently disabled feature is a support ticket."""
    with caplog.at_level("WARNING"):
        assert analysis_autostart.arm_from_environ(
            {"MUSIC_DJ_AUTO_ANALYZE": "on"}
        ) is False
    assert any(
        "analysis" in r.message.lower() and "not armed" in r.message.lower()
        for r in caplog.records
    ), f"the refusal was not explained: {[r.message for r in caplog.records]}"


def test_a_manual_refresh_after_the_drain_does_not_erase_the_booking(
    app, client, tmp_path
):
    """The booking must survive another job claiming the one slot.

    ``_JOBS`` holds exactly one job. If the user starts an ordinary
    library-wide refresh after the auto-drain finishes but before the next
    60-second tick, that job replaces the drain in the registry - and a
    library job publishes no ``queue_signature`` at all, because only the
    unmapped scope reads that queue. Booking whatever job happens to be
    current at tick time would therefore record ``None``, drop the
    suppression, and relaunch the same failed backlog immediately. The
    watcher holds on to the drain it started instead of looking one up.
    """
    _seed(app, "junk0001", _audio(tmp_path, "junk.mp3"))
    _enable_all_steps(client)
    watcher = _watcher(app)

    assert watcher.tick() == "started"
    _wait(client)
    consumed = ingest_mod._JOBS.current.queue_signature
    assert consumed, "the unmapped drain did not publish the queue it read"

    # A library-wide refresh takes the slot. Analysis-only so it stays quick;
    # the point is which job is current, not what the job achieves.
    client.put("/api/v1/ingest/config",
               json={"enabled": {"analysis": True, "stems": False,
                                 "vocals": False}})
    assert client.post("/api/v1/ingest/refresh", json={}).status_code == 202
    _wait(client)
    assert ingest_mod._JOBS.current.scope == "library", (
        "fixture precondition: the manual job must have taken the slot"
    )
    assert ingest_mod._JOBS.current.queue_signature is None, (
        "fixture precondition: a library job must publish no queue signature, "
        "or this test is not discriminating"
    )

    assert watcher.tick() == "unchanged", (
        "the booking was read off whichever job was current, so the failed "
        "backlog relaunched the moment an unrelated job took the slot"
    )
    assert app.state.auto_analyze.last_signature == consumed

pytestmark = pytest.mark.rb_parity
