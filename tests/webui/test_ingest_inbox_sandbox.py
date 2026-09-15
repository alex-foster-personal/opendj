"""POST /ingest/upload must not escape MDT_DATA_DIR (issue #3071).

Run in a subprocess so import-time INGEST_INBOX resolution matches production.
The child gets its own HOME so "did it write under fake home" is answered by
a directory this test owns.

Regression line:
  - if a sandboxed engine receives an ingest upload then no file is written
    outside its MDT_DATA_DIR tree
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_MP3 = REPO_ROOT / "tests" / "fixtures" / "phase7-dedup" / "src-128.mp3"

_PROBE = """
import json
import os
from pathlib import Path

from fastapi import FastAPI
from starlette.testclient import TestClient

from apps.shared.state.db import open_rw as open_state_rw
from apps.webui.server.routes import ingest as ingest_mod
from apps.webui.server.routes import ingest_upload as ingest_upload_mod

data_dir = Path(os.environ["MDT_DATA_DIR"])
home_dir = Path(os.environ["HOME"])
fixture = Path(os.environ["FIXTURE_MP3"])
state_db = data_dir / "state" / "state.db"
open_state_rw(state_db).close()

app = FastAPI()
app.include_router(ingest_upload_mod.router, prefix="/api/v1")
app.state.state_db = state_db

with TestClient(app) as client:
    response = client.post(
        "/api/v1/ingest/upload",
        files=[("files", (fixture.name, fixture.read_bytes(), "audio/mpeg"))],
        data={"batch": "adv-sandbox-batch"},
    )

inbox = ingest_mod.INGEST_INBOX
batch_dir = inbox / "adv-sandbox-batch"
staged = list(batch_dir.rglob("*")) if batch_dir.exists() else []
home_music = list((home_dir / "Music").rglob("*")) if (home_dir / "Music").exists() else []

print(json.dumps({
    "status": response.status_code,
    "ingest_inbox": str(inbox),
    "expected_inbox": str(data_dir / "Manual Library" / "_ingest"),
    "staged_under_inbox": sorted(str(p.relative_to(inbox)) for p in staged),
    "under_fake_home_music": sorted(str(p) for p in home_music),
}))
"""


@pytest.fixture(scope="module")
def probe(tmp_path_factory: pytest.TempPathFactory) -> dict:
    data_dir = tmp_path_factory.mktemp("ingest-data")
    home_dir = tmp_path_factory.mktemp("ingest-home")
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "MDT_DATA_DIR": str(data_dir),
            "HOME": str(home_dir),
            "FIXTURE_MP3": str(FIXTURE_MP3),
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
            f"ingest inbox sandbox probe failed (exit {result.returncode})\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-4000:]}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.requires_audio_stack
def test_upload_stages_under_data_dir_inbox(probe: dict) -> None:
    assert probe["status"] == 200
    assert probe["ingest_inbox"] == probe["expected_inbox"]
    assert probe["staged_under_inbox"], (
        f"no staged files under inbox; found {probe['staged_under_inbox']}"
    )


@pytest.mark.requires_audio_stack
def test_upload_writes_nothing_under_fake_home_music(probe: dict) -> None:
    assert probe["under_fake_home_music"] == [], (
        "sandboxed ingest upload escaped MDT_DATA_DIR into fake HOME/Music: "
        f"{probe['under_fake_home_music']}"
    )
