"""engine_core's own /api/v1/health wrapper must await the (now async) legacy
handler, or the packaged app's health route 500s with
``AttributeError: 'coroutine' object has no attribute 'model_dump'`` and the
desktop shell SIGTERMs the "unhealthy" engine (CAT-05, apps/engine_core/app.py
``engine_health``/``_add_health_route``).

#5494 (fe0ec417d2) made ``apps.webui.server.routes.health.health`` an
``async def`` so it could run its one blocking sqlite read off the shared
threadpool without queuing behind it. ``engine_core.app`` wraps that handler
by calling it DIRECTLY -- not through FastAPI's own dependency/route
dispatch, which awaits an async callable transparently -- so a sync wrapper
that calls it unwrapped-but-unawaited receives a coroutine object instead of
a ``HealthOut``. The dev preview (``apps.webui.server`` served directly)
never exercises this wrapper at all, which is why #5494's own tests never
saw it: this file is specifically for the packaged/engine_core app shape.

Run in a SUBPROCESS, matching test_contract_rev.py / test_migrate_on_boot.py:
``create_app`` imports the legacy modules, which resolve their paths from
``MDT_DATA_DIR`` at import time; doing that inside the pytest process would
bind the whole session to one throwaway data dir.

[if] engine_health awaits the legacy health coroutine [then] GET /api/v1/health
on the engine_core app returns 200 with the real seeded track count, [else stop].

Mutation control (verified by hand, not as a second committed test): dropping
the `await` on the `legacy_health(...)` call inside `_add_health_route`
(apps/engine_core/app.py) and re-running this test makes it fail with the
exact `'coroutine' object has no attribute 'model_dump'` the live incident
hit. A synthetic monkeypatch of `legacy_health` cannot reproduce this
faithfully: `engine_health`'s own `await` would still await whatever the
patched stand-in returns, so the only honest mutation control is editing the
one real line and watching this test go red, as was done on demon-llama for
this change (and matches how the companion fix's own commit message
describes its guard: "red before, green after").
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared.state import schema as state_schema

pytestmark = pytest.mark.requirement("CAT-05")

REPO_ROOT = Path(__file__).resolve().parents[2]
_SEEDED_TRACK_COUNT = 2


def _seed_tracks(state_db_path: Path, count: int) -> None:
    state_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        state_schema.apply_migrations(conn)
        for i in range(count):
            stable_id = f"hp-async-{i:03d}"
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, title, "
                "content_hash, created_at, updated_at) "
                "VALUES (?, 'inferred', ?, ?, 't0', 't0')",
                (stable_id, stable_id, stable_id),
            )
        conn.commit()
    finally:
        conn.close()


# Real (fixed) behaviour: engine_health awaits legacy_health. Builds the real
# engine_core app over a data dir seeded with real tracks, so the response
# under test is the live SqliteBackend's own stats(), not a stub value.
_PROBE_FIXED = """
import json
import os
from pathlib import Path

from starlette.testclient import TestClient

from apps.engine_core.app import HEALTH_PATH, create_app
from apps.engine_core.config import EngineConfig

cfg = EngineConfig(data_dir=Path(os.environ["MDT_DATA_DIR"]))
app = create_app(cfg)

# SEC-01 (#2689): a bare subprocess never imports tests/conftest.py's
# TestClient default -- base_url must be explicit or the daemon host
# allowlist 403s every request.
with TestClient(app, base_url="http://127.0.0.1") as client:
    health = client.get(HEALTH_PATH)

print(json.dumps({
    "status_code": health.status_code,
    "body": health.json() if health.status_code == 200 else health.text,
}))
"""


def _run_probe(probe: str, data_dir: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env={**os.environ, "MDT_DATA_DIR": str(data_dir)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"probe subprocess exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_engine_health_returns_200_with_real_seeded_stats(tmp_path: Path) -> None:
    data_dir = tmp_path / "engine-data"
    _seed_tracks(data_dir / "state" / "state.db", _SEEDED_TRACK_COUNT)

    out = _run_probe(_PROBE_FIXED, data_dir)

    assert out["status_code"] == 200, out["body"]
    body = out["body"]
    assert body["status"] == "ok"
    # Real stats, not a stub: the count must match what was actually seeded
    # into this run's own state.db, not a hardcoded fixture value.
    assert body["state_db"]["tracks"] == _SEEDED_TRACK_COUNT
    assert "contract_rev" in body
