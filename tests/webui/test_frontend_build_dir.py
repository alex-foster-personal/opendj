"""DEVLOOP-02 frontend build-directory configuration regression tests.

- [if] MDT_FRONTEND_BUILD_DIR is set [then] the web UI app, settings route,
  and engine SPA mount all use it, otherwise the development loop is broken.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_frontend_build_dir_override_reaches_all_spa_computation_sites(
    tmp_path: Path,
) -> None:
    """The isolated process verifies import-time and mount-time resolution."""
    build_dir = tmp_path / "rebuilt-frontend"
    build_dir.mkdir()
    (build_dir / "index.html").write_text("<html></html>", encoding="utf-8")
    script = """
import json
from fastapi import FastAPI
from starlette.routing import Mount
import apps.engine_core.app as engine_app
from apps.webui.server.app import FRONTEND_BUILD_DIR
from apps.webui.server.routes.settings import _FRONTEND_BUILD_DIR

app = FastAPI()
engine_app._mount_spa(app)
mount = next(route for route in app.routes if isinstance(route, Mount))
print(json.dumps({
    "webui": str(FRONTEND_BUILD_DIR),
    "settings": str(_FRONTEND_BUILD_DIR),
    "engine": mount.app.directory,
}))
"""
    environment = {**os.environ, "MDT_FRONTEND_BUILD_DIR": str(build_dir)}
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    resolved_dirs = json.loads(result.stdout)
    assert resolved_dirs["webui"] == str(build_dir)
    assert resolved_dirs["settings"] == str(build_dir)
    assert resolved_dirs["engine"] == str(build_dir)
