"""PERFMODE-03 app posture parse, wire body, HTTP, and CLI.

Regression: missing app_posture key must default to prep; uppercase must not
silently coerce.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.app_posture_api import APP_POSTURE_PATH, add_app_posture_route
from apps.shared.app_posture import (
    GIG_LIBRARY_POLL_MS,
    GIG_PREFETCH_BYTES,
    GIG_PREFETCH_TRACKS,
    PREP_LIBRARY_POLL_MS,
    AppPosture,
    InvalidAppPosture,
    apply_posture_to_prefetch,
    apply_posture_to_workers,
    library_poll_ms,
    parse_posture,
    posture_wire,
    read_posture_from_prefs,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.requirement("PERFMODE-03")
def test_parse_posture_defaults_missing_to_prep() -> None:
    """[if] parse_posture gets None [then] it returns PREP, [else stop]."""
    assert parse_posture(None) == AppPosture.PREP


@pytest.mark.requirement("PERFMODE-03")
def test_parse_posture_rejects_invalid() -> None:
    """[if] parse_posture gets practice [then] it raises InvalidAppPosture, [else stop]."""
    with pytest.raises(InvalidAppPosture):
        parse_posture("practice")


@pytest.mark.requirement("PERFMODE-03")
def test_read_posture_from_prefs_defaults_prep(tmp_path: Path) -> None:
    """[if] ui-prefs is absent [then] read_posture_from_prefs returns PREP, [else stop]."""
    assert read_posture_from_prefs(tmp_path) == AppPosture.PREP


@pytest.mark.requirement("PERFMODE-03")
def test_read_posture_from_prefs_reads_gig(tmp_path: Path) -> None:
    """[if] ui-prefs stores gig [then] read_posture_from_prefs returns GIG, [else stop]."""
    prefs = tmp_path / "state"
    prefs.mkdir(parents=True)
    (prefs / "ui-prefs.json").write_text(
        json.dumps({"app_posture": "gig"}) + "\n",
        encoding="utf-8",
    )
    assert read_posture_from_prefs(tmp_path) == AppPosture.GIG


@pytest.mark.requirement("PERFMODE-03")
def test_prep_scalers() -> None:
    """[if] posture is Prep [then] poll and workers stay at tier defaults, [else stop]."""
    assert library_poll_ms(AppPosture.PREP) == PREP_LIBRARY_POLL_MS
    assert apply_posture_to_workers(16, AppPosture.PREP) == 16
    tracks, nbytes = apply_posture_to_prefetch(4, 48 * 1024 * 1024, AppPosture.PREP)
    assert tracks == 4
    assert nbytes == 48 * 1024 * 1024


@pytest.mark.requirement("PERFMODE-03")
def test_gig_scalers() -> None:
    """[if] posture is Gig [then] poll halves workers and floors prefetch, [else stop]."""
    assert library_poll_ms(AppPosture.GIG) == GIG_LIBRARY_POLL_MS
    assert apply_posture_to_workers(16, AppPosture.GIG) == 8
    assert apply_posture_to_workers(8, AppPosture.GIG) == 4
    tracks, nbytes = apply_posture_to_prefetch(6, 96 * 1024 * 1024, AppPosture.GIG)
    assert tracks == GIG_PREFETCH_TRACKS
    assert nbytes == GIG_PREFETCH_BYTES


@pytest.mark.requirement("PERFMODE-03")
def test_posture_wire_prep(tmp_path: Path) -> None:
    """[if] posture_wire runs with no prefs [then] it returns Prep scalers, [else stop]."""
    body = posture_wire(tmp_path)
    assert body["posture"] == "prep"
    assert body["label"] == "Prep"
    scalers = body["scalers"]
    assert scalers["library_poll_ms"] == PREP_LIBRARY_POLL_MS
    assert scalers["worker_divisor"] == 1
    assert scalers["prefetch_tracks_floor"] is None
    assert scalers["prefetch_bytes_floor"] is None


@pytest.mark.requirement("PERFMODE-03")
def test_posture_wire_gig(tmp_path: Path) -> None:
    """[if] ui-prefs is gig [then] posture_wire returns Gig floors, [else stop]."""
    prefs = tmp_path / "state"
    prefs.mkdir(parents=True)
    (prefs / "ui-prefs.json").write_text(
        json.dumps({"app_posture": "gig"}) + "\n",
        encoding="utf-8",
    )
    body = posture_wire(tmp_path)
    assert body["posture"] == "gig"
    assert body["label"] == "Gig"
    scalers = body["scalers"]
    assert scalers["library_poll_ms"] == GIG_LIBRARY_POLL_MS
    assert scalers["worker_divisor"] == 2
    assert scalers["prefetch_tracks_floor"] == GIG_PREFETCH_TRACKS
    assert scalers["prefetch_bytes_floor"] == GIG_PREFETCH_BYTES


@pytest.mark.requirement("PERFMODE-03")
def test_cli_main_prints_json(tmp_path: Path) -> None:
    """[if] app_posture CLI runs [then] it prints prep JSON on stdout, [else stop]."""
    import os
    import subprocess
    import sys

    env = {**os.environ, "MDT_DATA_DIR": str(tmp_path)}
    proc = subprocess.run(
        [sys.executable, "-m", "apps.engine_core.app_posture"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0
    body = json.loads(proc.stdout)
    assert body["posture"] == "prep"


@pytest.mark.requirement("PERFMODE-03")
def test_http_app_posture_route(tmp_path: Path) -> None:
    """[if] GET /api/v1/app-posture is called [then] it returns 200 with prep, [else stop]."""
    app = FastAPI()
    add_app_posture_route(app, data_dir=tmp_path)
    client = TestClient(app)
    response = client.get(APP_POSTURE_PATH)
    assert response.status_code == 200
    assert response.json()["posture"] == "prep"


@pytest.mark.requirement("PERFMODE-03")
def test_engine_registers_app_posture_before_spa_mount() -> None:
    """[if] engine app.py is read [then] app-posture mounts before SPA, [else stop]."""
    source = (REPO_ROOT / "apps/engine_core/app.py").read_text(encoding="utf-8")
    spa = source.index("_mount_spa(app)")
    assert source.index("add_app_posture_route(") < spa
