"""Acceptance tests for the red-team fixture library's rekordbox side.

[if] a vendor-mapped fixture track 500s on the listing, rb-meta or audio route
then every red-team pod and evidence capture on the fixture reads partial
failure as product behavior (found in PR #3676's evidence capture).

Like ``test_redteam_guardrails``, nothing here is monkeypatched: the probe is a
real child process with a pod's environment, booting the daemon's own app
factory against a freshly built fixture library.
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

from scripts import redteam_fixture_library as fixtures
from scripts import redteam_fixture_schema as fixture_schema

REPO_ROOT = Path(__file__).resolve().parents[2]


#----- helpers ----------------------------------------------------------------


def _run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120, check=False)


# Runs in a child with a pod's environment, booting the daemon's own app
# factory, so the fixture is read exactly as a pod's server reads it. Every
# track is recorded as (rb-meta status, vendor, file_exists, audio status): a 500 from
# sqlite3.OperationalError and a silent fall back to the local vendor are
# both visible, rather than one of them reading as "unmapped".
_READ_PATH_PROBE = """
import json, sys
from fastapi.testclient import TestClient
from apps.webui.server.app import create_process_app
from apps.webui.server.rb_vendor_pkg.track_rows import bulk_rb_meta

track_ids = json.loads(sys.argv[1])
with TestClient(create_process_app(), base_url="http://127.0.0.1") as client:
    listing = client.get("/api/v1/tracks", params={"limit": len(track_ids)})
    rb_meta = {}
    for stable_id in track_ids:
        response = client.get(f"/api/v1/tracks/{stable_id}/rb-meta")
        body = response.json() if response.status_code == 200 else {}
        audio_status = client.head(f"/api/v1/tracks/{stable_id}/audio").status_code
        rb_meta[stable_id] = [
            response.status_code, body.get("vendor"), body.get("file_exists"), audio_status
        ]
print(json.dumps({
    "listing_status": listing.status_code,
    "bulk": sorted(bulk_rb_meta(track_ids)),
    "rb_meta": rb_meta,
}))
"""


def _free_port() -> int:
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        return int(probe_socket.getsockname()[1])


def _pod_probe_environment(library: fixtures.FixtureLibrary, home: Path) -> dict[str, str]:
    """The pod allow list (mod.POD_ENV_KEYS) plus the import path and the port.

    The daemon refuses to boot without a port, which a pod's server is handed
    explicitly. Passing it here keeps the probe off the checkout's root ``.env``,
    which CI runners do not have.
    """
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "TMPDIR": str(home / "tmp"),
        "MDT_DATA_DIR": str(library.data_dir),
        "MDT_LIBRARY_MODE": "local",
        "PYTHONPATH": str(REPO_ROOT),
        "MUSIC_DJ_BACKEND_PORT": str(_free_port()),
    }


def test_fixture_library_serves_every_mapped_track_through_the_webui(tmp_path: Path) -> None:
    """The listing, rb-meta and audio routes serve every fixture track without a 500."""
    library = fixtures.build_fixture_library(tmp_path / "library")
    result = _run(
        [sys.executable, "-c", _READ_PATH_PROBE, json.dumps(library.track_ids)],
        _pod_probe_environment(library, tmp_path / "home"),
    )
    assert result.returncode == 0, f"probe crashed:\n{result.stderr}"
    probe = json.loads(result.stdout.strip().splitlines()[-1])
    mapped = [
        track
        for index, track in enumerate(library.track_ids, start=1)
        if index % fixtures.UNMAPPED_TRACK_STRIDE != 0
    ]
    unmapped = sorted(set(library.track_ids) - set(mapped))
    assert unmapped, "negative control needs at least one unmapped fixture track"
    assert probe["listing_status"] == 200, (
        "[if] the track listing 500s on the fixture library then its master.plain.db "
        f"schema drifted from bulk_rb_meta:\n{result.stderr[-4000:]}"
    )
    assert probe["bulk"] == sorted(mapped)
    failed = {
        track: status
        for track, status in probe["rb_meta"].items()
        if track in mapped and status != [200, "rekordbox", True, 200]
    }
    assert not failed, (
        "[if] rb-meta errors on a mapped fixture track then a rekordbox table or "
        f"column is missing: {failed}\n{result.stderr[-4000:]}"
    )
    assert {tuple(probe["rb_meta"][track]) for track in unmapped} == {(200, "local", True, 200)}


def test_committed_rekordbox_schema_matches_pyrekordbox() -> None:
    """The fixture's DDL snapshot is exactly what the renderer produces today."""
    assert fixtures.REKORDBOX_SCHEMA_PATH == fixture_schema.SCHEMA_PATH
    assert fixture_schema.SCHEMA_PATH.read_text() == fixture_schema.render_rekordbox_schema(), (
        "[if] the committed rekordbox schema differs from pyrekordbox then the fixture "
        "drifted; regenerate: uv run python -m scripts.redteam_fixture_schema"
    )


def test_fixture_master_db_is_rekordbox_shaped(tmp_path: Path) -> None:
    """Every fixture table and column exists in rekordbox's own schema."""
    library = fixtures.build_fixture_library(tmp_path / "library")
    models = rekordbox_tables.Base.metadata.tables
    conn = sqlite3.connect(str(library.master_db))
    try:
        fixture_tables = [
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        assert {"djmdContent", "djmdGenre", "djmdCue"} <= set(fixture_tables)
        for table in fixture_tables:
            assert table in models, f"[if] fixture invents table {table} then not rekordbox-shaped"
            fixture_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            invented = fixture_columns - {column.name for column in models[table].columns}
            assert not invented, (
                f"[if] fixture {table} invents columns rekordbox lacks then it is not "
                f"rekordbox-shaped: {sorted(invented)}"
            )
    finally:
        conn.close()
