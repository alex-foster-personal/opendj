"""contract_rev determinism, and the health response that advertises it.

Run in a SUBPROCESS on purpose. ``create_app`` imports the legacy modules,
which resolve their paths from ``MDT_DATA_DIR`` at import time; doing that
inside the pytest process would bind the whole session to a throwaway data
dir. A child process with its own env is the honest way to build the real
app twice.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.engine_core.contract import (
    CONTRACT_REV_CHARS,
    WS_SCHEMA_VERSION,
    compute_contract_rev,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = """
import json
from pathlib import Path

from starlette.testclient import TestClient

from apps.engine_core.app import HEALTH_PATH, PROGRESS_PREFIX, create_app
from apps.engine_core.config import EngineConfig

cfg = EngineConfig(data_dir=Path(__import__("os").environ["MDT_DATA_DIR"]))
first = create_app(cfg)
second = create_app(cfg)

with TestClient(first, base_url="http://127.0.0.1") as client:
    health = client.get(HEALTH_PATH)
    events_in_schema = HEALTH_PATH in client.get("/openapi.json").json()["paths"]

print(json.dumps({
    "rev_first": first.state.contract_rev,
    "rev_second": second.state.contract_rev,
    "boot_first": first.state.engine_boot_id,
    "boot_second": second.state.engine_boot_id,
    "health_status": health.status_code,
    "health_body": health.json(),
    "health_in_schema": events_in_schema,
    "progress_routes": [
        r.path for r in first.router.routes
        if getattr(r, "path", "").startswith(PROGRESS_PREFIX)
    ],
}))
"""


@pytest.fixture(scope="module")
def probe(tmp_path_factory: pytest.TempPathFactory) -> dict:
    data_dir = tmp_path_factory.mktemp("engine-data")
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "MDT_DATA_DIR": str(data_dir),
            "MDT_LIBRARY_MODE": "local",
        }
    )
    env.pop("WEB_CONCURRENCY", None)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"engine app probe failed (exit {result.returncode})\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-4000:]}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_contract_rev_is_stable_across_two_app_builds(probe: dict) -> None:
    assert probe["rev_first"] == probe["rev_second"]
    assert probe["boot_first"] != probe["boot_second"], (
        "boot_id must be fresh per build; a stable contract_rev must not be "
        "coming from a cached app"
    )


def test_contract_rev_shape(probe: dict) -> None:
    rev = probe["rev_first"]
    assert len(rev) == CONTRACT_REV_CHARS
    assert rev == rev.lower()
    assert all(char in "0123456789abcdef" for char in rev)


def test_health_reports_the_contract(probe: dict) -> None:
    assert probe["health_status"] == 200
    body = probe["health_body"]
    assert body["contract_rev"] == probe["rev_first"]
    assert body["boot_id"] == probe["boot_first"]
    assert body["engine_version"]
    # The legacy payload must still be there: this is an extension, not a
    # replacement.
    assert body["status"] == "ok"
    assert "state_db" in body
    assert "bind_host" in body
    assert probe["health_in_schema"]


def test_progress_router_is_mounted(probe: dict) -> None:
    routes = sorted(probe["progress_routes"])
    assert any(route.endswith("/progress") for route in routes)
    assert any(route.endswith("/progress/schema") for route in routes)
    assert any("/progress/nodes/" in route for route in routes)


def test_contract_rev_changes_when_the_schema_or_ws_version_changes() -> None:
    base = {"openapi": "3.1.0", "paths": {"/a": {}}}
    widened = {"openapi": "3.1.0", "paths": {"/a": {}, "/b": {}}}
    assert compute_contract_rev(base) != compute_contract_rev(widened)
    # Key order is not a contract change; content is.
    assert compute_contract_rev(base) == compute_contract_rev(
        {"paths": {"/a": {}}, "openapi": "3.1.0"}
    )
    assert WS_SCHEMA_VERSION == "ws1"
