"""Tests for the analyze-on-import surface: queue, drain job and auto-drain.

Hermetic but REAL: the state.db is built by the production migrations, and
``MDT_DATA_DIR`` points the drain's subprocess at the same tmp data dir, so
the drain executes the production ``apps.analysis.run`` CLI end to end. The
failure test feeds it a file that is genuinely not decodable audio, so the
error it surfaces is a real non-zero exit and not a simulated one.

Regression lines:
  - if GET /analysis-queue counts a rekordbox-mapped track as pending then broken
  - if a djay-only mapping keeps a track out of the queue then broken
  - if POST /analysis-queue/run can start while a refresh is running then broken
  - if the unmapped drain targets mapped or already-analyzed tracks then broken
  - if the unmapped drain tries to run the id-keyed stems/vocals steps then broken
  - if an undecodable queued file reports phase=done then broken
  - if scope=unmapped is accepted together with batch_dir then broken
  - if scope=unmapped is accepted while the analysis step is disabled then broken
  - if the browser client calls an analysis-queue URL the app does not serve then broken
  - if the queue reads a different state DB from the one the backend serves then broken

A target that vanishes between the scan and the CLI's admission check is
covered in test_autodrain_booking.py, where it can be driven through the
real route and worker rather than through a hand-built job.
"""
from __future__ import annotations

import inspect
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError

from apps.analysis import run as analysis_run
from apps.analysis.backends import DEFAULT_BACKEND
from apps.webui.server.routes import ingest as ingest_mod
from tests.webui.analysis.conftest import (
    FIXTURES,
    _audio,
    _enable_all_steps,
    _seed,
    _wait,
)

API_INGEST_TS = (
    Path(__file__).resolve().parents[3]
    / "apps" / "webui" / "frontend" / "src" / "lib" / "rb" / "api-ingest.ts"
)

# ----- queue read surface ---------------------------------------------------

@pytest.mark.requirement("PARITY-06")
def test_queue_lists_only_unmapped_unanalyzed_tracks(client, app, tmp_path):
    _seed(app, "mapped01", _audio(tmp_path, "m.mp3"), mapped=True)
    _seed(app, "done0001", _audio(tmp_path, "d.mp3"), analyzed=True)
    _seed(app, "pend0001", _audio(tmp_path, "p.mp3"))
    _seed(app, "gone0001", tmp_path / "missing.mp3")

    out = client.get("/api/v1/analysis-queue").json()
    assert [i["stable_id"] for i in out["items"]] == ["pend0001"]
    assert out["pending"] == 1
    assert out["analyzed"] == 1
    assert out["unreachable"] == 1
    assert out["unmapped"] == 3
    assert out["job"]["phase"] == "idle"


@pytest.mark.requirement("PARITY-06")
def test_queue_still_lists_a_track_mapped_only_to_another_vendor(client, app, tmp_path):
    """A djay- or serato-only mapping supplies no ANLZ, so the track has no
    beatgrid to serve and is exactly what this queue is for. Only a rekordbox
    mapping excludes."""
    _seed(app, "djay0001", _audio(tmp_path, "d.mp3"), mapped=True, vendor="djay")
    _seed(app, "rbox0001", _audio(tmp_path, "r.mp3"), mapped=True)

    out = client.get("/api/v1/analysis-queue").json()
    assert [i["stable_id"] for i in out["items"]] == ["djay0001"]
    assert out["unmapped"] == 1


def test_queue_limit_pages_items_without_moving_the_counts(client, app, tmp_path):
    for sid in ("a0000001", "b0000001", "c0000001"):
        _seed(app, sid, _audio(tmp_path, f"{sid}.mp3"))

    out = client.get("/api/v1/analysis-queue?limit=2").json()
    assert [i["stable_id"] for i in out["items"]] == ["a0000001", "b0000001"]
    assert out["pending"] == 3


def test_queue_rejects_a_nonsense_limit(client):
    assert client.get("/api/v1/analysis-queue?limit=0").status_code == 422


def test_queue_reports_the_auto_drain_state(client, app):
    out = client.get("/api/v1/analysis-queue").json()
    assert out["auto"]["enabled"] is False
    assert out["auto"]["attempts"] == 0
    assert out["auto"]["last_signature"] is None


# ----- scope validation -----------------------------------------------------

def test_unmapped_scope_rejects_a_batch_dir(client):
    batch = ingest_mod.INGEST_INBOX / "b1"
    batch.mkdir(parents=True)
    r = client.post("/api/v1/ingest/refresh",
                    json={"scope": "unmapped", "batch_dir": str(batch)})
    assert r.status_code == 422
    assert "batch_dir" in r.text


def test_unmapped_scope_rejects_an_unknown_scope(client):
    r = client.post("/api/v1/ingest/refresh", json={"scope": "frobnicate"})
    assert r.status_code == 422


@pytest.mark.requirement("PARITY-06")
def test_unmapped_scope_requires_the_analysis_step(client):
    client.put("/api/v1/ingest/config",
               json={"enabled": {"analysis": False, "stems": True, "vocals": False}})
    r = client.post("/api/v1/ingest/refresh", json={"scope": "unmapped"})
    assert r.status_code == 422
    assert "analysis" in r.text


def test_run_is_rejected_while_a_refresh_is_running(client, app, tmp_path):
    _seed(app, "junk0001", _audio(tmp_path, "junk.mp3"))
    _enable_all_steps(client)
    assert client.post("/api/v1/analysis-queue/run").status_code == 202
    assert client.post("/api/v1/analysis-queue/run").status_code == 409
    _wait(client)


# REQ: LIBUX-11
def test_ordering_one_missing_analysis_creates_a_track_scoped_real_job(client, app, tmp_path):
    """The hover action and an HTTP agent order share this narrow, real drain."""
    _seed(app, "order001", _audio(tmp_path, "order.mp3"))
    _enable_all_steps(client)

    ordered = client.post("/api/v1/analysis-queue/orders/order001/beatgrid")
    assert ordered.status_code == 202
    ordered_body = ordered.json()
    assert ordered_body["stable_id"] == "order001"
    assert ordered_body["kind"] == "beatgrid"
    assert ordered_body["phase"] in {"queued", "running"}

    status = client.get("/api/v1/analysis-queue/orders/order001")
    assert status.status_code == 200
    items = status.json()["items"]
    assert len(items) == 1
    assert items[0]["stable_id"] == "order001"
    assert items[0]["kind"] == "beatgrid"
    assert items[0]["phase"] in {"queued", "running"}
    duplicate = client.post("/api/v1/analysis-queue/orders/order001/beatgrid")
    assert duplicate.status_code == 202
    assert duplicate.json() == items[0]
    competing = client.post("/api/v1/analysis-queue/orders/order001/key")
    assert competing.status_code == 409
    _wait(client)


@pytest.mark.parametrize("kind", ["unknown", "cues", "waveform", "phrase", "other"])
# REQ: LIBUX-11
def test_order_rejects_an_unknown_or_unproducible_analysis_kind(client, app, tmp_path, kind):
    """A 202 is reserved for a command that can materialize the requested dot."""
    _seed(app, "order002", _audio(tmp_path, "order.mp3"))
    _enable_all_steps(client)

    response = client.post(f"/api/v1/analysis-queue/orders/order002/{kind}")
    assert response.status_code == 422
    assert "cannot be ordered" in response.text


def test_order_rejects_a_missing_track_before_acknowledging(client):
    """A 202 must mean the requested track entered the shared job."""
    _enable_all_steps(client)

    response = client.post("/api/v1/analysis-queue/orders/no-such-track/beatgrid")

    assert response.status_code == 422
    assert "not an on-disk library track" in response.text


def test_track_refresh_input_rejects_an_unknown_kind_and_non_track_fields(tmp_path):
    """Malformed track requests must not become a library-wide refresh."""
    from apps.webui.server.routes.ingest_scope import RefreshIn, resolve_scope

    with pytest.raises(ValidationError, match="analysis_kind"):
        RefreshIn(scope="track", stable_id="order004", analysis_kind="beatgird")
    with pytest.raises(HTTPException, match="require scope='track'"):
        resolve_scope(RefreshIn(stable_id="order004"), tmp_path)


@pytest.mark.parametrize(("kind", "step"), [("stems", "stems"), ("vocals", "vocals")])
def test_track_order_runs_its_requested_coarse_step(client, app, tmp_path, kind, step):
    """A track order must not silently run the generic analysis worker."""
    _seed(app, f"{kind}001", _audio(tmp_path, f"{kind}.mp3"))
    _enable_all_steps(client)

    response = client.post(f"/api/v1/analysis-queue/orders/{kind}001/{kind}")

    assert response.status_code == 202
    assert response.json()["phase"] in {"queued", "running", "done", "error"}
    assert ingest_mod._JOBS.current is not None
    assert ingest_mod._JOBS.current.steps == [step]


def test_analysis_order_phase_rejects_unknown_job_states():
    """The analysis-order wire contract only permits frontend-renderable phases."""
    from apps.webui.server.routes.analysis_queue import AnalysisOrderOut

    with pytest.raises(ValueError, match="phase"):
        AnalysisOrderOut(stable_id="order003", kind="beatgrid", phase="mystery")


# ----- the drain ------------------------------------------------------------

@pytest.mark.requirement("PARITY-06")
def test_drain_targets_the_backlog_and_skips_the_id_keyed_steps(client, app, tmp_path):
    """A file of zero bytes is not decodable audio anywhere, so the production
    runner exits non-zero on every machine. The job must still have targeted
    exactly the backlog and skipped stems/vocals before it failed."""
    _seed(app, "mapped01", _audio(tmp_path, "m.mp3"), mapped=True)
    _seed(app, "done0001", _audio(tmp_path, "d.mp3"), analyzed=True)
    _seed(app, "junk0001", _audio(tmp_path, "junk.mp3"))
    _enable_all_steps(client)

    r = client.post("/api/v1/analysis-queue/run")
    assert r.status_code == 202
    assert r.json()["steps"] == ["analysis"]
    status = _wait(client)

    assert status["step_total"] == 1, status["log_tail"]
    assert any("1 of 2 rekordbox-unmapped" in ln for ln in status["log_tail"]), \
        status["log_tail"]
    assert any("stems: skipped for unmapped scope" in ln for ln in status["log_tail"])
    assert any("vocals: skipped for unmapped scope" in ln for ln in status["log_tail"])
    assert status["phase"] == "error", status["log_tail"]
    # The production CLI really ran and really failed. Asserted through the
    # log rather than through the error text, because the text names WHICH
    # failure it was and that legitimately differs by machine: a box with the
    # analysis extra fails to decode the file, a box without it fails on the
    # absent backend. Both are real, unstubbed failures of the same run.
    assert any("$ -m apps.analysis.run" in ln for ln in status["log_tail"]), \
        status["log_tail"]
    assert status["error"], "a failed drain must say why"


@pytest.mark.requirement("PARITY-06")
@pytest.mark.requires_audio_stack
def test_drain_writes_analysis_rows_under_the_canonical_id(client, app):
    """End to end: the queued unmapped track gets a real analysis row keyed by
    the state-layer stable_id, so /beatgrid-fallback has something to serve."""
    _seed(app, "local001", FIXTURES / "src-128.mp3")
    _enable_all_steps(client)

    assert client.post("/api/v1/analysis-queue/run").status_code == 202
    status = _wait(client)
    assert status["phase"] == "done", status["log_tail"]

    ids = {
        r[0] for r in sqlite3.connect(app.state.state_db).execute(
            "SELECT stable_id FROM analysis"
        )
    }
    assert ids == {"local001"}, f"analysis stored under non-canonical ids: {ids}"
    assert client.get("/api/v1/analysis-queue").json()["pending"] == 0


# ----- browser client <-> production route contract -------------------------

@pytest.mark.requirement("PARITY-06")
def test_the_browser_client_calls_a_route_the_app_really_serves(app):
    """The Svelte client builds its analysis-queue URL by hand, so a renamed
    or re-prefixed route would 404 in a browser with every test still green.
    Read the URLs out of the client source and require the PRODUCTION app to
    mount each one.

    Deliberately not a fetch-level client test: a browser test that installs
    its own fetch can only prove the string it was handed, never that the
    daemon answers it, and the repo's test contract bars that fabricated
    success outright (AGENTS.md, "No mocks and locked real fixtures").
    """
    source = API_INGEST_TS.read_text()
    get_call = re.search(
        r"getTrackAnalysisOrders.*?fetch\(`\$\{API_BASE\}"
        r"(?P<path>/api/v1/analysis-queue/orders/\$\{encodeURIComponent\(stableId\)\})`\)",
        source,
        flags=re.DOTALL,
    )
    post_call = re.search(
        r"orderTrackAnalysis.*?fetch\(\s*`\$\{API_BASE\}"
        r"(?P<path>/api/v1/analysis-queue/orders/\$\{encodeURIComponent\(stableId\)\}"
        r"/\$\{encodeURIComponent\(kind\)\})`,\s*\{ method: 'POST' \}",
        source,
        flags=re.DOTALL,
    )
    assert get_call is not None, f"missing GET order call in {API_INGEST_TS.name}"
    assert post_call is not None, f"missing POST order call in {API_INGEST_TS.name}"
    client_routes = {
        ("GET", re.sub(r"\$\{encodeURIComponent\(stableId\)\}", "{stable_id}", get_call["path"])),
        ("POST", re.sub(r"\$\{encodeURIComponent\(kind\)\}", "{kind}", re.sub(
            r"\$\{encodeURIComponent\(stableId\)\}", "{stable_id}", post_call["path"]
        ))),
    }

    served = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert client_routes <= served, (
        f"the client calls routes the app does not serve: {sorted(client_routes - served)}"
    )
    assert ("POST", "/api/v1/analysis-queue/run") in served, (
        "the agent-facing run endpoint must stay mounted even with no TS client"
    )


# ----- one file for the queue, the served backend and the drain -------------

@pytest.mark.requirement("PARITY-06")
def test_the_queue_reads_the_same_file_the_served_backend_does():
    """``unmapped_backlog`` resolves its state DB the way the backend does.

    ``open_ro()`` with no argument opens ``apps.shared.paths.STATE_DB``, and
    that is deliberate rather than an oversight. It is the SAME constant
    ``make_backend()`` falls back to and the same one the drain's subprocess
    re-derives from ``MDT_DATA_DIR``, so selection, serving and writes are one
    file by construction.

    It is specifically NOT bound to ``app.state.state_db_path``. ``_build_default_app``
    now passes ``STATE_DB`` explicitly there too (#949), so in practice they
    agree, but the queue does not rely on that: binding it to the app-state
    string would make the scan follow whatever a future caller passes there,
    which is a knob this reader has no business honoring.

    This test is the guard on that reasoning. If either default moves, the
    queue and the backend split and this reds instead of shipping a scan that
    reports on a library nobody is serving.
    """
    from apps.shared import paths as shared_paths
    from apps.shared.state import paths as state_paths
    from apps.webui.server import sqlite_backend

    assert state_paths.STATE_DB is shared_paths.STATE_DB

    served = sqlite_backend.make_backend()
    assert getattr(served, "_path", shared_paths.STATE_DB) == shared_paths.STATE_DB, (
        "make_backend() no longer defaults to paths.STATE_DB, so the queue's "
        "own default has stopped tracking the file the app serves"
    )

    assert inspect.signature(ingest_mod.unmapped_backlog).parameters.keys() == {
        "limit"
    }, (
        "unmapped_backlog grew a DB-selecting parameter. That is allowed, but "
        "its callers must then all pass the SAME file make_backend() serves "
        "and the drain subprocess re-derives from MDT_DATA_DIR - update this "
        "guard to assert that, do not just delete it"
    )


@pytest.mark.requires_audio_stack
def test_a_failing_chunk_does_not_block_the_chunks_behind_it(tmp_path, monkeypatch):
    """One undecodable file must not starve every track sorted after it.

    ``_step_analysis`` walks the sorted targets in ``ANALYSIS_CHUNK`` slices.
    When an early chunk exited non-zero it used to abort the whole step, so
    nothing later was ever attempted: no analysis rows appeared, the backlog
    signature never moved, and the reconcile loop then read that unchanged
    signature as "already tried this queue" and suppressed the retry. A
    handful of permanently unanalyzable files could therefore block the
    entire library behind them, forever.

    The failure here is real, not simulated: these are files whose bytes are
    genuinely not decodable audio, so ``apps.analysis.run`` exits non-zero on
    its own. The step must still end in an exception - the job has to report
    error - but only AFTER every chunk has had its turn.
    """
    # The chunk runner spawns a real subprocess that re-derives its paths
    # from the environment, so without this it would write into the repo's
    # own data/ dir and leave a state.db behind that un-skips the
    # live-library tests in tests/reconcile.
    monkeypatch.setenv("MDT_DATA_DIR", str(tmp_path / "data"))
    targets = []
    for i in range(ingest_mod.ANALYSIS_CHUNK + 1):   # spans two chunks
        path = tmp_path / f"t{i:03d}.mp3"
        path.write_text("this is definitively not audio")
        targets.append((f"sid{i:03d}", str(path)))

    job = ingest_mod._RefreshJob(started_at=0.0, steps=["analysis"], scope="unmapped")
    with pytest.raises(RuntimeError) as caught:
        ingest_mod._step_analysis(job, targets)

    # Which chunks were attempted is read off the raised error, not off
    # job.log. The log is a deque(maxlen=LOG_RING) that a real analyzer run
    # overflows on its own - librosa alone emits a PySoundFile-fallback
    # warning and three mpg123 resync notes per undecodable file, so the
    # second chunk's stdout evicts the first chunk's invocation line and a
    # count over the ring reports "1 chunk attempted" for a step that plainly
    # attempted two. The accumulated error is produced by the production
    # code, is not truncated, and names every chunk offset it tried.
    failed_offsets = re.findall(r"chunk at (\d+):", str(caught.value))
    assert failed_offsets == ["0", str(ingest_mod.ANALYSIS_CHUNK)], (
        "expected both chunks to be attempted and both to fail, got: "
        f"{caught.value}"
    )
    assert not job.recently_done_ids, (
        "a chunk that failed must not report its ids as done"
    )


def test_a_backend_that_cannot_run_stops_the_job_at_the_first_chunk(
    tmp_path, monkeypatch
):
    """A capability failure is not a per-file failure and must not be swept.

    Continuing past a failed chunk exists so a few undecodable files cannot
    starve the valid tracks sorted behind them. When the BACKEND itself
    cannot run - the packaged engine ships without librosa and scipy, and the
    TopBar can still start a manual refresh - every chunk fails for the same
    reason, and continuing means one subprocess per chunk across the whole
    library before reporting a failure that was knowable at the first.

    The unavailability is real, not simulated. ``mik`` needs the paid
    ``mixed-in-key-cli`` binary on PATH, which this box does not have (the
    test asserts that as a precondition), so the production CLI raises
    BackendNotAvailable for real and really reports it as a capability
    failure.

    It is NAMED, not substituted: ``_step_analysis`` takes the backend as an
    argument for exactly this, so the module's shipped choice stays where it
    is and can be asserted alongside. What this does not do is exercise the
    packaged engine's own missing-extra path - that needs a box where the
    shipped backend is genuinely absent, and every environment this suite
    runs in installs it (see the guard below). The chunk-loop behavior is
    what is under test here, and it is identical for both.
    """
    if shutil.which("mixed-in-key-cli") is not None:
        pytest.skip(
            "UNAVAILABLE: mixed-in-key-cli is on PATH here, so 'mik' can "
            "really run and cannot stand in for an absent backend"
        )
    assert ingest_mod.ANALYSIS_BACKEND == DEFAULT_BACKEND, (
        "the drain no longer ships the default backend, so the message this "
        "test reads is about a backend nothing runs"
    )
    monkeypatch.setenv("MDT_DATA_DIR", str(tmp_path / "data"))
    targets = []
    for i in range(ingest_mod.ANALYSIS_CHUNK + 1):   # spans two chunks
        path = tmp_path / f"c{i:03d}.mp3"
        path.write_text("this is definitively not audio")
        targets.append((f"cid{i:03d}", str(path)))

    job = ingest_mod._RefreshJob(started_at=0.0, steps=["analysis"], scope="unmapped")
    job.queue_signature = "signature-of-the-queue-the-scan-read"

    with pytest.raises(RuntimeError) as caught:
        ingest_mod._step_analysis(job, targets, backend="mik")

    attempted = [
        line for line in job.log if "$ " in line and "apps.analysis.run" in line
    ]
    assert len(attempted) == 1, (
        f"a backend that cannot run was retried per chunk, saw {len(attempted)}"
    )
    assert "backend" in str(caught.value).lower(), (
        f"the error must name the capability failure, got: {caught.value}"
    )
    assert job.queue_signature == "signature-of-the-queue-the-scan-read", (
        "the signature is not cleared here: a machine with no backend never "
        "arms the reconcile loop, so there is no automatic retry to protect"
    )


def test_a_systemic_cli_failure_stops_the_job_at_the_first_chunk(
    tmp_path, monkeypatch
):
    """Only a PER-TARGET exit may leave the chunk loop running.

    Continuing exists so a few undecodable files cannot starve the valid
    tracks behind them. Anything that is not about these files - an argparse
    or configuration error, a signal, a crashed worker pool, a SQLITE_FULL on
    the state DB - recurs identically on every chunk, so continuing means
    decoding the whole library to meet the same wall N/25 times.

    Reproduced with a real configuration error rather than a simulated one:
    the backend name is not registered, so the production CLI really exits 2
    from argparse. The name is passed as an ARGUMENT, so the module's shipped
    backend is left where it is and nothing is replaced.
    """
    monkeypatch.setenv("MDT_DATA_DIR", str(tmp_path / "data"))
    targets = []
    for i in range(ingest_mod.ANALYSIS_CHUNK + 1):   # spans two chunks
        path = tmp_path / f"s{i:03d}.mp3"
        path.write_text("this is definitively not audio")
        targets.append((f"sid{i:03d}", str(path)))

    job = ingest_mod._RefreshJob(started_at=0.0, steps=["analysis"], scope="unmapped")

    with pytest.raises(RuntimeError) as caught:
        ingest_mod._step_analysis(job, targets, backend="no-such-backend")

    attempted = [
        line for line in job.log if "$ " in line and "apps.analysis.run" in line
    ]
    assert len(attempted) == 1, (
        f"a systemic failure was retried per chunk, saw {len(attempted)}"
    )
    assert f"exited {analysis_run.EXIT_USAGE}" in str(caught.value), (
        f"the error must carry the exit code it stopped on, got: {caught.value}"
    )


def test_a_storage_failure_stops_the_job_at_the_first_chunk(tmp_path, monkeypatch):
    """An exception that escapes the run is systemic, whatever CPython says.

    CPython exits 1 for an escaping exception, and 1 is EXIT_TRACK_FAILURES -
    the one status the chunk loop is allowed to continue past. So before the
    CLI grew a boundary, a state DB that could not be opened, a full disk or a
    dead worker pool arrived at the caller wearing the same status as "a few
    files would not decode", and the drain worked through the whole library
    meeting the identical wall once per chunk.

    The failure is real and it is the storage one: MDT_DATA_DIR is pointed at
    a path whose parent is a regular FILE, so the production state-DB open
    raises NotADirectoryError from the kernel. Nothing is patched and no
    failure is simulated - the analyzer really cannot open its database.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a regular file where the data dir wants a parent")
    monkeypatch.setenv("MDT_DATA_DIR", str(blocker / "data"))
    targets = []
    for i in range(ingest_mod.ANALYSIS_CHUNK + 1):   # spans two chunks
        path = tmp_path / f"s{i:03d}.mp3"
        path.write_text("this is definitively not audio")
        targets.append((f"sid{i:03d}", str(path)))

    job = ingest_mod._RefreshJob(started_at=0.0, steps=["analysis"], scope="unmapped")

    with pytest.raises(RuntimeError) as caught:
        ingest_mod._step_analysis(job, targets)

    attempted = [
        line for line in job.log if "$ " in line and "apps.analysis.run" in line
    ]
    assert len(attempted) == 1, (
        f"a storage failure was retried per chunk, saw {len(attempted)}"
    )
    assert str(analysis_run.EXIT_INTERNAL_ERROR) in str(caught.value), (
        f"the error must carry the exit code it stopped on, got: {caught.value}"
    )
    assert any("NotADirectoryError" in line for line in job.log), (
        "the underlying fault was swallowed rather than logged"
    )

pytestmark = pytest.mark.rb_parity
