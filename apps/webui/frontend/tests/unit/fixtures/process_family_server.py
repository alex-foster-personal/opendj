"""Real HTTP server for process-family-snapshot-fetch.test.mjs (PERF-UI-6785).

Codex flagged the earlier version of that test BLOCKING for serving
hard-coded available and unavailable bodies from a node handler. Every body
the JS test now reads comes from the production route,
``GET /api/v1/performance/telemetry/processes``, mounted by the production
``create_app`` and fed only real inputs:

- the live process walk is psutil's, rooted either at this server's own pid
  (a real family, so the route answers ``available: true``) or at a real
  child that has run, exited and been reaped (a genuinely empty walk, so it
  answers ``available: false``), through the route's documented
  ``app.state.process_family_root_pid`` seam;
- the native-probe record is a real capture: ``tests/fixtures/perf/
  probe-samples.jsonl``, two consecutive samples taken verbatim from the
  installed launchd probe's own daily log, checksum-verified against
  ``(private path)`` before use and copied byte for byte
  into a day file the production reader (``_latest_process_record``) finds.
  Nothing in it is built or restamped here: it is stale because it was
  captured on Tue 1 Sep 2026, so the route's own clock calls it stale.

Test controls, under ``/test/``, arrange inputs around the real route:
``POST /test/family/{self|exited}`` re-roots the walk; each
``POST /test/hold-next`` makes one more processes response wait, AFTER the
route has computed it, and ``POST /test/release`` lets the oldest waiting one
go, so the JS test can make real requests overlap and arrive in any order;
``GET /test/held`` counts responses waiting; ``POST /test/probe/fresh?age_s=N``
adds a FRESH record, N seconds old, of the locked capture with each row's pid
moved onto a real helper (readings unchanged); ``POST /test/probe/captured``
goes back to the verbatim capture alone and ``POST /test/probe/none`` removes
every probe record; ``POST /test/delay-next?s=N`` makes the next processes
request wait N seconds BEFORE the route runs, as a slow walk would (so the
wait is inside the probe age the route computes); ``POST /test/drift-next?mode=M``
rewrites the next processes body, the route's real one, into a contract drift
(``empty`` -> ``{}``, ``string-available`` -> ``available`` as the string
``"true"``/``"false"``, ``string-stale`` -> ``stale`` as a string,
``null-member`` -> a ``null`` appended to ``members``, ``string-rss`` -> the
first member's ``rss_mb`` as a string), the one control that does touch a
body, because a malformed body is exactly what the client must survive and
the production route never sends one; ``POST /test/shutdown`` stops the server
(portable, see stop-fixture-server.mjs).

Before ``READY`` the server prints ``SCRATCH <dir>``: its scratch directory,
which it removes on the way out WITHOUT suppressing errors, so a failed
cleanup is a nonzero exit (stop-fixture-server.mjs fails the test on it) and
the JS test checks the directory is really gone.

The listening socket is bound here and handed to uvicorn with
``Server.run(sockets=[...])``, which works on Windows too (``Config(fd=...)``
goes through ``socket.fromfd``, which Windows lacks).

Usage: ``uv run --no-sync python process_family_server.py``, then read the
single ``READY <port>`` stdout line.
"""

from __future__ import annotations

import asyncio
import copy
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from apps.diagnostics.probe_types import utc_now
from scripts.diagnostics.probe_log_store import append_bounded_jsonl
from tests.perf.captures import REAL_PROBE_SAMPLE, _verified_bytes
from tests.real_process_tree import kill, node, spawn

_SCRATCH_DIR = Path(tempfile.mkdtemp(prefix="process-family-server-"))
PROCESSES_PATH = "/api/v1/performance/telemetry/processes"
CAPTURE_NAME = "probe-samples.jsonl"
# The day file name the probe's sampling loop writes and the route globs for.
CAPTURE_DAY_FILE = "opendj-performance-2026-09-01.jsonl"

# captured probe pid -> live helper pid (engine in-family, others detached)
_CAPTURED_TO_REAL: dict[int, int] = {}
_SPAWNED_FOR_KILL: list[dict[str, int]] = []


def _exited_pid() -> int:
    """The pid of a real child that has run, exited and been reaped."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    if child.wait(timeout=20) != 0:
        raise RuntimeError("the exited-root child did not exit cleanly")
    return child.pid


def _install_captured_probe_log(directory: Path) -> None:
    """Copy the locked real probe capture, verified first, into the log dir."""
    (directory / CAPTURE_DAY_FILE).write_bytes(_verified_bytes(CAPTURE_NAME))


def _start_probe_helpers() -> None:
    """Spawn titled helpers: one in-family engine row, five detached probe rows."""
    for row in REAL_PROBE_SAMPLE["processes"]:
        captured_pid = int(row["pid"])
        command = str(row["command"])
        key = f"probe_{captured_pid}"
        pids = (
            spawn(node(key, command))
            if row["role"] == "python-engine"
            else spawn(node(key, command, detach=True))
        )
        _CAPTURED_TO_REAL[captured_pid] = pids[key]
        _SPAWNED_FOR_KILL.append(pids)


def _reap_probe_helpers() -> None:
    for pids in _SPAWNED_FOR_KILL:
        kill(pids)
    _SPAWNED_FOR_KILL.clear()
    _CAPTURED_TO_REAL.clear()


def _write_fresh_capture(directory: Path, age_s: float) -> None:
    """The verified capture's second sample, fresh, with real pids on each row."""
    if not _CAPTURED_TO_REAL:
        raise RuntimeError("probe helpers were not started")
    record = copy.deepcopy(REAL_PROBE_SAMPLE)
    for row in record["processes"]:
        row["pid"] = _CAPTURED_TO_REAL[int(row["pid"])]
    for moved, captured in zip(record["processes"], REAL_PROBE_SAMPLE["processes"], strict=True):
        if {k: v for k, v in moved.items() if k != "pid"} != {
            k: v for k, v in captured.items() if k != "pid"
        }:
            raise RuntimeError("a relocated row differs from the capture")
    stamped = datetime.fromisoformat(utc_now().replace("Z", "+00:00")) - timedelta(seconds=age_s)
    record["timestamp"] = (
        stamped.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    _path, stored = append_bounded_jsonl(directory, record, cap_bytes=1_048_576)
    if not stored:
        raise RuntimeError("the probe writer refused the record")


def _add_probe_controls(app: FastAPI) -> None:
    @app.post("/test/probe/fresh")
    def _probe_fresh(age_s: float) -> dict[str, float]:
        _write_fresh_capture(_SCRATCH_DIR, age_s)
        return {"age_s": age_s}

    @app.post("/test/probe/none")
    def _probe_none() -> dict[str, str]:
        """No probe record at all, as before the probe ever wrote."""
        for path in _SCRATCH_DIR.glob("opendj-performance-*.jsonl"):
            path.unlink()
        return {"probe": "none"}

    @app.post("/test/probe/captured")
    def _probe_captured() -> dict[str, str]:
        """Back to the verbatim (stale) capture alone."""
        for path in _SCRATCH_DIR.glob("opendj-performance-*.jsonl"):
            if path.name != CAPTURE_DAY_FILE:
                path.unlink()
        _install_captured_probe_log(_SCRATCH_DIR)
        return {"probe": "captured"}


def _drift_empty(_body: dict[str, Any]) -> dict[str, Any]:
    return {}


def _drift_string_available(body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "available": str(body["available"]).lower()}


def _drift_string_stale(body: dict[str, Any]) -> dict[str, Any]:
    if "stale" not in body:
        raise RuntimeError("string-stale needs a body that carries `stale`")
    return {**body, "stale": str(body["stale"]).lower()}


def _members_of(body: dict[str, Any], mode: str) -> list[Any]:
    members = body.get("members")
    if not isinstance(members, list) or not members:
        raise RuntimeError(f"{mode} needs an available body with members")
    return members


def _drift_empty_members(body: dict[str, Any]) -> dict[str, Any]:
    _members_of(body, "empty-members")
    return {**body, "members": []}


def _drift_one_member(body: dict[str, Any]) -> dict[str, Any]:
    first, *_ = _members_of(body, "one-member")
    return {**body, "members": [first]}


def _drift_negative_mb(body: dict[str, Any]) -> dict[str, Any]:
    first, *rest = _members_of(body, "negative-mb")
    return {**body, "members": [{**first, "rss_mb": -1.0}, *rest]}


def _drift_zero_mb(body: dict[str, Any]) -> dict[str, Any]:
    first, *rest = _members_of(body, "zero-mb")
    return {**body, "members": [{**first, "rss_mb": 0.0, "physical_footprint_mb": 0.0}, *rest]}


def _drift_null_member(body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "members": [*_members_of(body, "null-member"), None]}


def _drift_string_rss(body: dict[str, Any]) -> dict[str, Any]:
    first, *rest = _members_of(body, "string-rss")
    return {**body, "members": [{**first, "rss_mb": str(first.get("rss_mb"))}, *rest]}


_DRIFTS = {
    "empty": _drift_empty,
    "empty-members": _drift_empty_members,
    "one-member": _drift_one_member,
    "negative-mb": _drift_negative_mb,
    "zero-mb": _drift_zero_mb,
    "null-member": _drift_null_member,
    "string-rss": _drift_string_rss,
    "string-available": _drift_string_available,
    "string-stale": _drift_string_stale,
}


async def _drifted(response: Any, mode: str) -> JSONResponse:
    """The route's real body, read back and rewritten by ``mode``."""
    import json

    if response.status_code != 200:
        raise RuntimeError(f"cannot drift a {response.status_code} response")
    raw = b"".join([chunk async for chunk in response.body_iterator])
    return JSONResponse(_DRIFTS[mode](json.loads(raw)))


def _add_one_shot_controls(app: FastAPI, once: dict[str, Any]) -> None:
    @app.post("/test/delay-next")
    def _delay_next(s: float) -> dict[str, float]:
        once["delay_s"] = s
        return {"delay_s": s}

    @app.post("/test/drift-next")
    def _drift_next(mode: str) -> dict[str, str]:
        if mode not in _DRIFTS:
            raise ValueError(mode)
        once["drift"] = mode
        return {"drift": mode}


def create_server_app() -> FastAPI:
    from apps.webui.server.app import create_app
    from apps.webui.server.backend import InMemoryBackend

    app = create_app(backend=InMemoryBackend(), mount_frontend=False, enable_cors=False)
    app.state.performance_process_log_dirs = (_SCRATCH_DIR,)
    # No shell registers here: the scratch dir holds no .engine.parent.
    app.state.shell_registration_dir = _SCRATCH_DIR
    app.state.process_family_root_pid = None
    exited = _exited_pid()
    _install_captured_probe_log(_SCRATCH_DIR)
    _start_probe_helpers()
    # Armed holds and the responses waiting on them, oldest first.
    hold: dict[str, Any] = {"armed": 0, "waiting": []}
    # One-shot: a pre-route delay, and a body drift, for the next processes request.
    once: dict[str, Any] = {"delay_s": 0.0, "drift": None}

    @app.middleware("http")
    async def _hold_processes(request: Request, call_next):  # type: ignore[no-untyped-def]
        is_processes = request.url.path == PROCESSES_PATH
        if is_processes and once["delay_s"] > 0:
            delay_s, once["delay_s"] = once["delay_s"], 0.0
            await asyncio.sleep(delay_s)  # before the route: inside the age it computes
        response = await call_next(request)  # the route has run: the body is computed
        if is_processes and once["drift"] is not None:
            mode, once["drift"] = once["drift"], None
            response = await _drifted(response, mode)
        if is_processes and hold["armed"] > 0:
            hold["armed"] -= 1
            released = asyncio.Event()
            hold["waiting"].append(released)
            await released.wait()
        return response

    @app.post("/test/family/{root}")
    def _family(root: str) -> dict[str, object]:
        if root not in ("self", "exited"):
            raise ValueError(root)
        app.state.process_family_root_pid = None if root == "self" else exited
        return {"root": root}

    _add_probe_controls(app)
    _add_one_shot_controls(app, once)

    @app.post("/test/hold-next")
    def _hold_next() -> dict[str, int]:
        hold["armed"] += 1
        return {"armed": hold["armed"]}

    @app.get("/test/held")
    def _held() -> dict[str, int]:
        return {"held": len(hold["waiting"])}

    @app.post("/test/release")
    async def _release() -> dict[str, int]:
        """Let the OLDEST held response go."""
        if hold["waiting"]:
            hold["waiting"].pop(0).set()
        return {"held": len(hold["waiting"])}

    @app.post("/test/shutdown")
    def _shutdown() -> dict[str, bool]:
        app.state.server.should_exit = True
        return {"stopping": True}

    return app


def _serve() -> None:
    app = create_server_app()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()  # before READY, so an immediate connect is queued, not refused
    port = sock.getsockname()[1]
    print(f"SCRATCH {_SCRATCH_DIR}", flush=True)
    print(f"READY {port}", flush=True)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    app.state.server = server
    server.run(sockets=[sock])


def main() -> int:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        _serve()
    finally:
        _reap_probe_helpers()
        # No ignore_errors: a cleanup that fails (a handle left open, a file
        # still being written) must fail the run, not leak into the next one.
        shutil.rmtree(_SCRATCH_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
