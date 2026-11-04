"""A real shell-launches-engine process pair, for the default-walk tests.

``shell`` mode stands in for the Electron main process: it starts two
Chromium-style children (``--type=renderer`` and ``--type=gpu-process``), waits
until both show those command lines, then starts ``engine`` mode as its child
the way ``apps/desktop/electron/src/engine.ts`` ``spawnEngine`` does, with its
OWN pid in ``OPENDJ_PARENT_PID`` (or, with ``--no-stamp``, with the variable
removed, as a CLI engine runs). It relays the engine's one JSON line, adds the
pids it started, and stops its children.

``engine`` mode is the engine: inside this child, with nothing passed in, it
runs the production walk and the production ``/processes`` route, exactly as
the engine does, so the shell pid comes from the environment it inherited.

Usage: ``python electron_launch_stand_in.py shell [--no-stamp]``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

_SLEEP = "import time; time.sleep(120)"
_CHILD_TYPES = {"renderer": "--type=renderer", "gpu": "--type=gpu-process"}


def _wait_for_argv(pid: int, token: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if token in psutil.Process(pid).cmdline():
                return
        except psutil.Error:
            pass
        time.sleep(0.05)
    raise RuntimeError(f"pid {pid} never showed {token}")


def _shell(stamp: bool) -> int:
    children = {
        key: subprocess.Popen([sys.executable, "-c", _SLEEP, token])
        for key, token in _CHILD_TYPES.items()
    }
    try:
        for key, token in _CHILD_TYPES.items():
            _wait_for_argv(children[key].pid, token)
        env = dict(os.environ)
        env.pop("OPENDJ_PARENT_PID", None)
        if stamp:
            env["OPENDJ_PARENT_PID"] = str(os.getpid())
        engine = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "engine"],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if engine.returncode != 0:
            sys.stderr.write(engine.stderr)
            return engine.returncode
        report = json.loads(engine.stdout.strip().splitlines()[-1])
        report["shell"] = os.getpid()
        report.update({key: child.pid for key, child in children.items()})
        print(json.dumps(report), flush=True)
        return 0
    finally:
        for child in children.values():
            child.kill()
            child.wait(timeout=10)


def _engine() -> int:
    from fastapi.testclient import TestClient

    from apps.shared.machine_pressure import live_process_family_state
    from apps.webui.server.app import create_app
    from apps.webui.server.backend import InMemoryBackend

    members, pids = live_process_family_state()
    app = create_app(backend=InMemoryBackend(), mount_frontend=False, enable_cors=False)
    with tempfile.TemporaryDirectory(prefix="electron-stand-in-") as empty_log_dir:
        app.state.performance_process_log_dirs = (Path(empty_log_dir),)
        # This engine was spawned, not adopted: no registration, only the stamp.
        app.state.shell_registration_dir = Path(empty_log_dir)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            body = client.get("/api/v1/performance/telemetry/processes").json()
    report = {
        "engine": os.getpid(),
        "inherited": os.environ.get("OPENDJ_PARENT_PID"),
        "family": sorted(pids),
        "roles": {str(m["pid"]): m.get("role") for m in members},
        "route": body,
    }
    print(json.dumps(report), flush=True)
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["engine"]:
        return _engine()
    if argv[:1] == ["shell"]:
        return _shell(stamp="--no-stamp" not in argv[1:])
    raise SystemExit("usage: electron_launch_stand_in.py shell [--no-stamp] | engine")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
