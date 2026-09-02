"""Standalone HTTP parity tests for issue #205 USB export.

The router is intentionally mounted on a local FastAPI instance because the
fan-out contract reserves ``apps/webui/server/app.py`` for the integrator.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sync.usb.pioneer import export_workflow as workflow
from apps.sync.usb.pioneer import writer_rbox
from apps.webui.server.app import create_app
from apps.webui.server.routes import usb_export
from tests.fixtures.conftest import resolve_required_fixture

# Live-write MECHANICS against tmp fixtures: runs with the one-way rekordbox
# import gate ON (root conftest reads the marker). Never a real rb target.
pytestmark = [pytest.mark.requirement("CAT-06"), pytest.mark.rekordbox_writeback]


def _fixture_db() -> Path:
    """Resolve ``tests/fixtures/rb-usb-export/PIONEER/rekordbox/exportLibrary.db`` lazily.

    Routes through resolve_required_fixture() (rather than a hard-coded
    repo path) so this CAT-06 acceptance module fails closed on a missing
    fixture host instead of silently breaking, once the in-repo directory
    leaves and only ``rb-usb-export.extern`` remains (PR #718). Deferred
    out of a module-level constant into this helper (called only from the
    fixture-dependent test bodies below) so an
    ``MDT_ALLOW_MISSING_FIXTURES=1`` skip -- or a missing host with no
    opt-out, which fails closed via ``resolve_required_fixture`` -- drops
    only the tests that actually need USB data, not the whole module
    (PR #718 review).
    """
    return (
        resolve_required_fixture("rb-usb-export") / "PIONEER" / "rekordbox" / "exportLibrary.db"
    )


def _promote_exclusively_on_test_filesystem(
    source: Path, destination: Path
) -> None:
    """Exercise real no-replace promotion without claiming host USB support."""
    os.link(source, destination)
    source.unlink()


@pytest.fixture
def target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "DISPOSABLE-API"
    root.mkdir()
    (root / workflow.DISPOSABLE_MARKER_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "disposable": True,
                "volume_label": root.name,
                "volume_uuid": "USB-API",
                "authorization_id": "api-test",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        workflow,
        "inspect_macos_target",
        lambda path: workflow.TargetIdentity(
            root=Path(path).resolve(),
            volume_label=root.name,
            volume_uuid="USB-API",
            authorization_id="api-test",
        ),
    )
    monkeypatch.setattr(
        workflow, "_rename_exclusive", _promote_exclusively_on_test_filesystem
    )
    return root


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(usb_export.router, prefix="/api/v1")
    return TestClient(app)


def _require_rbox_runtime() -> None:
    """Fail HTTP parity at the USB runtime dependency contract boundary."""
    if not writer_rbox.RBOX_AVAILABLE:
        pytest.fail(
            "USB export HTTP parity requires the pinned runtime dependency "
            "rbox==0.1.7. Install the repository dependency contract before "
            f"running these tests: {writer_rbox.RBOX_IMPORT_ERROR}",
            pytrace=False,
        )


def test_production_app_registers_all_usb_export_contracts() -> None:
    """Agent and UI callers share the routes registered by the production app."""
    paths = create_app(mount_frontend=False).openapi()["paths"]

    assert "/api/v1/usb-export/plan" in paths
    assert "/api/v1/usb-export/apply" in paths
    assert "/api/v1/usb-export/readback" in paths


def test_http_plan_apply_readback_matches_core_schema(
    client: TestClient, target: Path
) -> None:
    _require_rbox_runtime()
    plan_response = client.post(
        "/api/v1/usb-export/plan",
        json={
            "template_path": str(_fixture_db()),
            "target_root": str(target),
            "playlists": [{"name": "HTTP 205", "track_ids": [1, 2]}],
            "track_updates": [{"id": 1, "title": "HTTP title", "rating": 4}],
        },
    )
    assert plan_response.status_code == 200
    plan = plan_response.json()
    assert plan["scope"] == "onelibrary_overlay_only"

    apply_response = client.post(
        "/api/v1/usb-export/apply",
        json={"plan": plan, "confirmation": plan["plan_id"]},
    )
    assert apply_response.status_code == 200
    receipt = apply_response.json()
    assert receipt["verified"] is True

    readback_response = client.post(
        "/api/v1/usb-export/readback",
        json={"plan": plan, "receipt": receipt},
    )
    assert readback_response.status_code == 200
    report = readback_response.json()
    assert report["verified"] is True
    assert report["playlists"][0]["track_ids"] == [1, 2]


def test_http_errors_are_structured_and_fail_closed(
    client: TestClient, target: Path
) -> None:
    response = client.post(
        "/api/v1/usb-export/plan",
        json={
            "template_path": str(_fixture_db()),
            "target_root": str(target),
            "playlists": [],
            "track_updates": [],
        },
    )
    plan = response.json()
    (target / "existing.txt").write_text("valuable", encoding="utf-8")

    response = client.post(
        "/api/v1/usb-export/apply",
        json={"plan": plan, "confirmation": plan["plan_id"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "target_not_empty"
    assert (target / "existing.txt").read_text(encoding="utf-8") == "valuable"


def test_standalone_openapi_lists_all_workflow_operations(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/usb-export/plan" in paths
    assert "/api/v1/usb-export/apply" in paths
    assert "/api/v1/usb-export/readback" in paths
