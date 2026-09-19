"""PREFLIGHT-02 / issue #2722 P1-3: rekordbox installed but library unreachable.

[if] the rekordbox app folder exists but master.db is missing [then] preflight surfaces unreachable remediation, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared import platform_paths
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.preflight_checks import LIBRARY_ATTACHED_REKORDBOX_UNREACHABLE_REMEDIATION

pytestmark = pytest.mark.requirement("PREFLIGHT-02")


def _build_client(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, *, rb_app_dir: Path
) -> TestClient:
    state_path = data_dir / "state" / "state.db"
    monkeypatch.setattr(platform_paths, "rekordbox_app_dir", lambda: rb_app_dir)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", data_dir / "absent-master.db")
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
        state_db_path=str(state_path),
    )
    return TestClient(app, raise_server_exceptions=False)


def test_fresh_install_rekordbox_unreachable_offers_folder_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rb_dir = tmp_path / "pioneer-rekordbox"
    rb_dir.mkdir()
    with _build_client(monkeypatch, tmp_path / "data", rb_app_dir=rb_dir) as client:
        body = client.get("/api/v1/preflight").json()
    row = next(c for c in body["checks"] if c["id"] == "library-attached")
    assert row["status"] == "fail"
    assert "Import a folder instead" in row["remediation"]
    assert LIBRARY_ATTACHED_REKORDBOX_UNREACHABLE_REMEDIATION.split("{")[0] in row["remediation"]
