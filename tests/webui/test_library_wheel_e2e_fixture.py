"""The library-wheel e2e fixture serves every route its page load reaches.

[if] the extended library-wheel e2e's fixture library 500s on the track
listing or the playlist listing then the page records failed resources and
library-wheel.spec.ts fails at its failed-resource assertion, only in the
nightly extended job (run 36510204674: ``no such table: djmdPlaylist`` from
``playlist_order_index``, ``no such column: c.ImagePath`` from
``bulk_rb_meta``). This catches the same drift in the fast lane.

Nothing is monkeypatched: the fixture is built by the exact CLI the Playwright
config runs, and the probe is a child process booting the daemon's own app
factory against that data dir, so the engine reads it as the e2e engine does.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

from pyrekordbox.db6 import tables as rekordbox_tables

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_MODULE = "apps.webui.frontend.tests.e2e.support.library_wheel_fixture"

# The requests /library-wheel's page load makes against the engine, plus the
# playlist axis the spec switches to.
_PAGE_ROUTES = (
    "/api/v1/tracks?limit=500",
    "/api/v1/playlists?availability=skip",
    "/api/v1/library/wheel",
    "/api/v1/library/wheel?axis=playlist",
)

_ROUTE_PROBE = """
import json, sys
from fastapi.testclient import TestClient
from apps.webui.server.app import create_process_app

with TestClient(create_process_app(), base_url="http://127.0.0.1",
                raise_server_exceptions=False) as client:
    out = {}
    for route in json.loads(sys.argv[1]):
        response = client.get(route)
        ok = response.status_code == 200
        out[route] = [response.status_code, response.json() if ok else None]
print(json.dumps(out))
"""


#----- helpers ----------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        return int(probe_socket.getsockname()[1])


def _engine_environment(data_dir: Path, home: Path) -> dict[str, str]:
    """The e2e engine's env (playwright.library-wheel.config.ts) plus a port."""
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "TMPDIR": str(home / "tmp"),
        "MDT_DATA_DIR": str(data_dir),
        "MDT_LIBRARY_MODE": "local",
        "PYTHONPATH": str(REPO_ROOT),
        "MUSIC_DJ_BACKEND_PORT": str(_free_port()),
    }


def _build_fixture(data_dir: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", FIXTURE_MODULE, "--data-dir", str(data_dir)],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=120, check=False,
    )
    assert result.returncode == 0, f"fixture build failed:\n{result.stderr}"
    assert (data_dir / "master.plain.db").is_file()


#----- tests ------------------------------------------------------------------


def test_library_wheel_e2e_fixture_serves_every_page_route(tmp_path: Path) -> None:
    """Tracks, playlists and both wheel axes answer 200 with the fixture's library."""
    data_dir = tmp_path / "library-wheel-e2e-data"
    _build_fixture(data_dir)
    result = subprocess.run(
        [sys.executable, "-c", _ROUTE_PROBE, json.dumps(_PAGE_ROUTES)],
        capture_output=True, text=True, cwd=tmp_path, timeout=120, check=False,
        env=_engine_environment(data_dir, tmp_path / "home"),
    )
    assert result.returncode == 0, f"probe crashed:\n{result.stderr}"
    probe = json.loads(result.stdout.strip().splitlines()[-1])

    failed = {route: status for route, (status, _body) in probe.items() if status != 200}
    assert not failed, (
        "[if] a /library-wheel page route errors on the e2e fixture then its "
        f"master.plain.db schema drifted from the engine's readers: {failed}\n"
        f"{result.stderr[-4000:]}"
    )
    tracks = probe["/api/v1/tracks?limit=500"][1]
    assert sorted(row["stable_id"] for row in tracks["items"]) == [
        "t-house-1", "t-no-rb-1", "t-techno-1", "t-techno-2", "t-unmatched-1",
    ]
    playlists = probe["/api/v1/playlists?availability=skip"][1]
    assert [row["name"] for row in playlists] == ["Warmup"]
    wheel = probe["/api/v1/library/wheel"][1]
    assert wheel["total_tracks"] == 5
    assert len(wheel["families"]) == 2


def test_library_wheel_e2e_master_db_is_rekordbox_shaped(tmp_path: Path) -> None:
    """Every table the engine reads exists with rekordbox's own columns, none invented."""
    data_dir = tmp_path / "library-wheel-e2e-data"
    _build_fixture(data_dir)
    models = rekordbox_tables.Base.metadata.tables
    conn = sqlite3.connect(str(data_dir / "master.plain.db"))
    try:
        fixture_tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"djmdContent", "djmdGenre", "djmdPlaylist"} <= fixture_tables
        for table in sorted(fixture_tables):
            assert table in models, f"[if] fixture invents table {table} then not rekordbox-shaped"
            fixture_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            model_columns = {column.name for column in models[table].columns}
            assert fixture_columns == model_columns, (
                f"[if] fixture {table} columns differ from rekordbox's then it is stripped "
                f"or invented: missing {sorted(model_columns - fixture_columns)}, "
                f"invented {sorted(fixture_columns - model_columns)}"
            )
    finally:
        conn.close()
