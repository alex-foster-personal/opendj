"""Real HTTP fixture server for usb-import-wait.test.mjs (USBIMP-01).

This binds a REAL uvicorn server on an OS-assigned loopback port and mounts
the production routers the USB import path talks to:

  * ``POST /api/v1/setup/import/folder`` (apps.engine_core.setup.api), which
    queues the real ``setup.import-rekordbox`` folder-mode job;
  * ``/api/v1/jobs`` (apps.engine_core.jobs.api), read by ``getSetupJob`` and
    used by the failure test to enqueue a folder import the setup route would
    refuse up front;
  * ``POST /api/v1/analysis-queue/run`` (apps.webui.server.routes.analysis_queue),
    the drain ``startAnalysisQueueDrain`` issues.

A real ``JobRunner`` spawns the real ``apps.engine_core.setup.worker``
subprocess, which walks a real temp "stick" folder holding a COPY of one track
from the committed acceptance corpus (tests/fixtures/app-acceptance; the
corpus itself is only read) and writes the track into a scratch state.db.

Everything this process writes lives under one per-run scratch dir OUTSIDE the
tracked tree, and ``MDT_DATA_DIR`` points there BEFORE any ``apps`` import, so
the drain's backlog read (``apps.shared.paths.STATE_DB``) is the same scratch
state.db the import wrote, never a real library or the checkout's data/.

``/test/requests`` returns every API request this process served, in order,
recorded by real ASGI middleware. A job read carries the status it actually
answered, and the drain carries the files the production backlog held at the
moment it was asked, so the JS test can assert ordering without touching
``globalThis.fetch``.

Usage: ``uv run --no-sync python usb_import_wait_server.py``, then read the
single ``READY <port>`` stdout line.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

#: Per-run scratch dir OUTSIDE the tracked tree. Set before any ``apps``
#: import because apps.shared.platform_paths reads MDT_DATA_DIR at import time.
_SCRATCH_DIR = Path(tempfile.mkdtemp(prefix="usb-import-wait-server-"))
_DATA_DIR = _SCRATCH_DIR / "data"
(_DATA_DIR / "state").mkdir(parents=True)
os.environ["MDT_DATA_DIR"] = str(_DATA_DIR)

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request, Response  # noqa: E402

from apps.engine_core.config import EngineConfig  # noqa: E402
from apps.engine_core.jobs.api import router as jobs_router  # noqa: E402
from apps.engine_core.jobs.runner import JobRunner  # noqa: E402
from apps.engine_core.jobs.store import JobStore  # noqa: E402
from apps.engine_core.setup.api import router as setup_router  # noqa: E402
from apps.shared.paths import STATE_DB  # noqa: E402
from apps.webui.server.auto_analyze_prefs import DEFAULT_AUTO_LANES  # noqa: E402
from apps.webui.server.routes import ingest, ingest_cli_procs  # noqa: E402
from apps.webui.server.routes.analysis_queue import router as analysis_queue_router  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[6]
#: One real track from the committed acceptance corpus (fixture.json lists its
#: sha256). Copied, never moved or written.
_CORPUS_DIR = _REPO_ROOT / "tests/fixtures/app-acceptance"
_CORPUS_SCHEMA = "opendj-acceptance-fixture/v1"
_CORPUS_TRACK = _CORPUS_DIR / "library/01-acceptance-pulse.mp3"
_STICK_DIR = _SCRATCH_DIR / "stick"
#: Never created: the folder the failing import is asked to walk.
_MISSING_STICK_DIR = _SCRATCH_DIR / "stick-that-was-pulled"
#: How long shutdown waits for a drain this process started to finish, so its
#: analysis subprocess cannot write into the scratch dir after it is removed.
#: Shutdown stops the drain's CLIs first (ingest_cli_procs.stop_all), so this
#: only covers the job noticing; it plus the test's 15 s margin stays under the
#: runner's 120 s file timeout.
_DRAIN_SETTLE_S = 15.0

if STATE_DB != _DATA_DIR / "state" / "state.db":
    raise SystemExit(f"STATE_DB resolved to {STATE_DB}, not the scratch data dir {_DATA_DIR}")


def _verify_corpus_track() -> None:
    """Refuse a drifted corpus: the manifest's schema and this track's sha256 must match."""
    manifest = json.loads((_CORPUS_DIR / "fixture.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != _CORPUS_SCHEMA:
        raise SystemExit(f"acceptance corpus schema is {manifest.get('schema')!r}, not {_CORPUS_SCHEMA!r}")
    rel = _CORPUS_TRACK.relative_to(_CORPUS_DIR).as_posix()
    entries = [t for t in manifest["tracks"] if t.get("file") == rel]
    if len(entries) != 1:
        raise SystemExit(f"acceptance corpus manifest lists {rel} {len(entries)} times, not once")
    actual = hashlib.sha256(_CORPUS_TRACK.read_bytes()).hexdigest()
    if actual != entries[0]["sha256"]:
        raise SystemExit(f"{rel} sha256 is {actual}, manifest says {entries[0]['sha256']}")


def _prepare_stick() -> None:
    _verify_corpus_track()
    _STICK_DIR.mkdir()
    shutil.copyfile(_CORPUS_TRACK, _STICK_DIR / _CORPUS_TRACK.name)


def create_app() -> FastAPI:
    store = JobStore(_SCRATCH_DIR / "jobs.db", boot_id="usb-import-wait", owner_pid=os.getpid())
    store.recover()
    runner = JobRunner(store, poll_s=0.05)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        await runner.start()
        try:
            yield
        finally:
            await asyncio.to_thread(ingest_cli_procs.stop_all)
            await runner.stop()
            store.close()

    app = FastAPI(lifespan=_lifespan)
    app.state.engine_cfg = EngineConfig(data_dir=_DATA_DIR)
    app.state.jobs_store = store
    app.state.jobs_runner = runner
    # The drain resolves stem roots off app.state first; keep it in scratch.
    app.state.stem_roots = (_SCRATCH_DIR / "stems",)
    app.state.requests = []
    app.include_router(setup_router, prefix="/api/v1")
    app.include_router(jobs_router, prefix="/api/v1")
    app.include_router(analysis_queue_router, prefix="/api/v1")

    @app.middleware("http")
    async def _record_request(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/v1/"):
            return await call_next(request)
        entry: dict[str, object] = {"method": request.method, "path": path}
        if request.method == "POST" and path == "/api/v1/analysis-queue/run":
            # The production backlog the drain is about to read, read the same
            # way (ingest.unmapped_backlog), at the moment the drain arrives.
            # Before any import has run there is no state.db at all; that is
            # recorded as such rather than read, which would raise here and
            # turn an early drain into a 500 the route itself never answers.
            entry["backlog_files"] = (
                sorted(
                    Path(item.file_path).name
                    for item in ingest.unmapped_backlog(lanes=DEFAULT_AUTO_LANES).pending
                )
                if STATE_DB.exists()
                else "no state.db yet"
            )
        response = await call_next(request)
        entry["status"] = response.status_code
        if request.method == "GET" and path.startswith("/api/v1/jobs/"):
            # The status this read actually ANSWERED, from the bytes served,
            # not a second read of the store that could have moved on.
            body = b"".join([chunk async for chunk in response.body_iterator])
            if response.status_code == 200:
                entry["job_status"] = json.loads(body)["status"]
            response = Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )
        app.state.requests.append(entry)
        return response

    @app.get("/test/requests")
    def _get_requests() -> list[dict[str, object]]:
        return app.state.requests

    @app.get("/test/paths")
    def _get_paths() -> dict[str, str]:
        return {
            "data_dir": str(_DATA_DIR),
            "stick": str(_STICK_DIR),
            "missing_stick": str(_MISSING_STICK_DIR),
            "track": _CORPUS_TRACK.name,
        }

    @app.post("/test/shutdown")
    def _shutdown() -> dict[str, bool]:
        # Cooperative stop: `ChildProcess.kill()` on Windows terminates the
        # process outright, so neither the SIGTERM handler nor main()'s cleanup
        # would run.
        app.state.server.should_exit = True
        return {"stopping": True}

    return app


def _await_drain_settled() -> None:
    deadline = time.monotonic() + _DRAIN_SETTLE_S
    while time.monotonic() < deadline:
        job = ingest._JOBS.current
        if job is None or job.phase not in ingest.ACTIVE_PHASES:
            return
        time.sleep(0.1)
    raise RuntimeError(f"the analysis drain was still running {_DRAIN_SETTLE_S}s after shutdown")


def main() -> int:
    # Preferred stop is POST /test/shutdown (portable). A SIGTERM fallback still
    # cleans up on POSIX: uvicorn shuts down, restores the handler installed
    # before it, then re-raises the signal, and the default handler would skip
    # the cleanup.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        _serve()
        _await_drain_settled()
    finally:
        shutil.rmtree(_SCRATCH_DIR)
    return 0


def _serve() -> None:
    _prepare_stick()
    app = create_app()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    # listen() BEFORE the READY line, so a reader that connects the instant it
    # sees READY queues in the backlog instead of racing uvicorn's startup.
    sock.listen()
    port = sock.getsockname()[1]
    print(f"SCRATCH {_SCRATCH_DIR}", flush=True)
    print(f"READY {port}", flush=True)
    # sockets=, not Config(fd=...): fd reconstruction is Unix only.
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    app.state.server = server
    server.run(sockets=[sock])


if __name__ == "__main__":
    raise SystemExit(main())
