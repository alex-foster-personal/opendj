"""Shared real-fixture scaffolding for the analyze-on-import suites.

A conftest in its OWN package rather than an importable helper module: the
``app`` and ``client`` fixtures have to be visible to both suites without
being re-imported into each (which reads as a redefinition), and they must
not reach the rest of tests/webui, whose conftest already defines a very
different ``client``. A subpackage is the one arrangement that gives both.

It exists so test_queue.py (the route + queue surface) and test_autodrain.py
(the reconcile loop) share ONE definition of the production app under test
instead of two that drift apart.

Hermetic but REAL: the state.db is built by the production migrations, and
``MDT_DATA_DIR`` points the drain's subprocess at the same tmp data dir, so
the drain executes the production ``apps.analysis.run`` CLI end to end.
Nothing here replaces behavior under test.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.analysis import backlog as backlog_mod
from apps.analysis import store as analysis_store
from apps.shared.state import paths as state_paths
from apps.shared.state.db import open_rw as open_state_rw
from apps.webui.server import analysis_autostart
from apps.webui.server import app as app_mod
from apps.webui.server.routes import ingest as ingest_mod

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "phase7-dedup"
_TS = "2026-09-01T00:00:00Z"

@pytest.fixture
def app(tmp_path, monkeypatch):
    """The PRODUCTION app: routers, prefix, response models, exception
    handlers and ``app.state.auto_analyze`` all come from ``create_app``, so a
    router that stops being mounted or an auto-drain that stops being armed
    fails these tests rather than passing against a hand-built stand-in.

    The four ingest filesystem roots and its read connection are rebound to a
    tmp data dir because they are import-time constants (``apps.shared.paths``
    resolves them once, at import), so a per-test ``MDT_DATA_DIR`` cannot reach
    them. Nothing about the behavior under test is replaced: the state.db is
    created by the production migrations, ``MDT_DATA_DIR`` points the drain's
    subprocess at that same file, and the drain runs the real
    ``apps.analysis.run`` CLI. Same fixture main already uses for this router
    in tests/webui/test_ingest_routes.py.
    """
    data_dir = tmp_path / "data"
    state_db = data_dir / "state" / "state.db"
    open_state_rw(state_db).close()
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))

    monkeypatch.setattr(ingest_mod, "CONFIG_PATH", tmp_path / "ingest-config.json")
    monkeypatch.setattr(ingest_mod, "INGEST_INBOX", tmp_path / "_ingest")
    monkeypatch.setattr(ingest_mod, "VOCAL_CACHE_DIR", tmp_path / "vocal-cache")
    monkeypatch.setattr(ingest_mod, "DEFAULT_STEMS_DIR", tmp_path / "stems")
    # The read path is NOT replaced. Only where it looks is: `open_ro()`
    # resolves `state.paths.STATE_DB` at CALL time, so pointing that at the
    # tmp DB leaves the production accessor - its `mode=ro` URI and its
    # `query_only` pragma included - to do the opening.
    monkeypatch.setattr(state_paths, "STATE_DB", state_db)
    # BOTH handles, or a drain from an earlier test is still the newest
    # finished unmapped attempt this app can see.
    monkeypatch.setattr(ingest_mod._JOBS, "current", None)
    monkeypatch.setattr(ingest_mod._JOBS, "last_unmapped", None)

    app = app_mod.create_app(
        state_db_path=str(state_db), mount_frontend=False, port=18733,
        frontend_port=19733,
    )
    app.state.state_db = state_db
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _seed(app, sid, path, *, mapped=False, analyzed=False, vendor="rekordbox"):
    conn = sqlite3.connect(app.state.state_db)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
        "file_path, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (sid, "inferred", f"t-{sid}", "[]", str(path), _TS, _TS),
    )
    if mapped:
        conn.execute(
            "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) "
            "VALUES (?, ?, ?)",
            (sid, vendor, f"vid-{sid}"),
        )
    conn.commit()
    conn.close()
    if analyzed:
        conn = analysis_store.open_conn(app.state.state_db)
        conn.execute(
            "INSERT INTO analysis (stable_id, backend, backend_version, analyzed_at, "
            "duration_s, sample_rate, bpm, bpm_confidence, key_camelot, key_openkey, "
            "key_confidence, energy, energy_source, record_json) VALUES "
            "(?, 'librosa', 'seed', ?, 200.0, 44100, 120.0, 0.9, '8A', '1d', 0.9, 5, "
            "'seed', '{}')",
            (sid, _TS),
        )
        conn.commit()
        conn.close()


def _enable_all_steps(client):
    client.put("/api/v1/ingest/config",
               json={"enabled": {"analysis": True, "stems": True, "vocals": True}})


def _wait(client, timeout_s=600):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = client.get("/api/v1/ingest/refresh/status").json()
        if not status["running"]:
            return status
        time.sleep(0.25)
    raise AssertionError("drain still running")


def _audio(tmp_path, name):
    p = tmp_path / name
    p.write_bytes(b"\x00" * 4096)
    return p


def _watcher(app, *, enabled=True):
    """The watcher the daemon builds, not a re-declaration of it.

    Re-listing scan_fn / start_fn / running_fn here would keep
    every caller green after the daemon started draining the wrong scope, so
    they go through the production builder.
    """
    app.state.auto_analyze = analysis_autostart.build(enabled=enabled)
    return app_mod.build_auto_analyze_watcher(app)


#: Unreachable rows added to make a scan take real time. The scan resolves
#: and stats every unmapped track, so this is genuine work over a genuinely
#: large library, not a delay. Sized from measurement: ~8us/track locally,
#: so ~20k tracks buys ~150ms, and the fixture VERIFIES what it bought
#: rather than assuming it.
_SLOW_SCAN_ROWS: int = 20_000

@contextmanager
def _watching_for_the_production_scan():
    """Yield an Event that is set when a reconcile thread ENTERS the real scan.

    Replaces a fixed sleep. A sleep asserts nothing: on a loaded box the tick
    may not have reached its scan within the nap, and the case would then be
    exercising a stop BEFORE the scan - which a sibling case already covers -
    while still reporting green on the stop-DURING-scan race it is named
    after. Waiting on the event makes the ordering causal instead of hopeful.

    ``threading.settrace`` installs the tracer on threads started AFTER it, so
    the watcher's own thread carries it and NOTHING is injected into the
    watcher, the tick, or the scan. The target is the production
    ``backlog.scan`` code object, so this cannot drift onto a test double.

    The tracer returns None, so only ``call`` events fire and no line tracing
    is installed. Its cost lands on the traced thread and can only lengthen
    the scan, which widens the window this case needs rather than closing it.
    """
    entered = threading.Event()
    target = backlog_mod.scan.__code__

    def tracer(frame, event, arg):
        if frame.f_code is target:
            entered.set()
        # Falling off the end returns None, which is how CPython is told not
        # to install a local trace function - so only `call` events fire.

    # Restore whatever was there rather than clearing to None: under
    # pytest-cov the coverage tracer is already installed, and dropping it
    # would silently stop measuring every thread created by every test after
    # this one, which shows up as a coverage regression nobody can source.
    previous = threading.gettrace()
    threading.settrace(tracer)
    try:
        yield entered
    finally:
        threading.settrace(previous)


def _a_library_too_big_to_scan_quickly(app, tmp_path, *, min_window_s: float):
    """Seed a library whose real scan outlasts a short join, and prove it did.

    ``stop()`` joins with a timeout, so "shutdown arrives while a tick is
    still inside its scan" is the ordinary case on any library big enough to
    take a moment to walk - and every one of these cases is about what
    happens in that window. The window here is opened by real work: 20k
    tracks that the production scan really resolves and really stats, plus
    one materialized track so the backlog is genuinely non-empty and an
    unguarded tick would have a drain to start.

    Returns the measured scan duration. Skips as UNAVAILABLE, rather than
    quietly asserting nothing, on a machine fast enough that the window this
    test needs does not exist - the capability is the slow scan, and a
    machine without it cannot report on this race.

    Measured untraced and in this thread, so it is a conservative floor: the
    watcher thread runs the same scan under a call tracer and is therefore
    slower still.
    """
    conn = sqlite3.connect(app.state.state_db)
    try:
        conn.executemany(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, "
            "artists_json, file_path, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [(f"bulk{i:07d}", "inferred", f"t{i}", "[]", f"/nowhere/{i}.mp3",
              "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z")
             for i in range(_SLOW_SCAN_ROWS)],
        )
        conn.commit()
    finally:
        conn.close()
    _seed(app, "slow0001", _audio(tmp_path, "slow.mp3"))

    started = time.perf_counter()
    backlog = ingest_mod.unmapped_backlog()
    measured = time.perf_counter() - started

    assert backlog.pending_total == 1, (
        "fixture precondition: an unguarded tick must have work to start"
    )
    floor = 10 * min_window_s
    if measured < floor:
        pytest.skip(
            f"UNAVAILABLE: a real scan of {_SLOW_SCAN_ROWS} tracks took "
            f"{measured*1000:.0f}ms on this machine, under the {floor*1000:.0f}ms "
            "this race needs to be observable. This is a capability report, "
            "not a pass: the shutdown-during-scan window is unmeasured here."
        )
    return measured


@contextmanager
def _evicting_one_target_at_the_handoff(doomed):
    """Unlink ``doomed`` at the entry to the production chunk runner.

    That is where a real eviction lands: after the scan admitted the file and
    before the CLI performs its own existence check, which is the window
    ``EXIT_MISSING_TARGETS`` names. Deleting it any earlier just makes the scan
    report the track ``unreachable``, which is a different case entirely.

    ``threading.settrace`` installs on threads started after it, so the drain's
    worker carries this and nothing is injected into the route, the worker or
    the CLI.

    Shared, because both the booking module and the slot module need a drain
    that really loses a target; it is the only way either of them can produce
    a DELIBERATELY cleared queue signature.
    """
    chunking = ingest_mod._run_analysis_chunk.__code__

    def evict_once(frame, event, arg):
        if frame.f_code is chunking and doomed.exists():
            doomed.unlink()
        # Returning None declines a local trace, so only `call` events fire.

    previous = threading.gettrace()
    threading.settrace(evict_once)
    try:
        yield
    finally:
        threading.settrace(previous)
