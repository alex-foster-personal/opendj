"""Structured HTTP errors for stems hydrate and push-missing (STEM-33, issue #2914)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.webui.test_stems_hydration import _assets_client


@pytest.mark.requirement("STEM-33")
def test_hydrate_missing_manifest_path_returns_structured_400(tmp_path: Path) -> None:
    with _assets_client(data_dir=tmp_path / "data") as client:
        resp = client.post(
            f"/api/v1/stems/{'a' * 40}/hydrate",
            json={"manifest_path": "advtest", "dry_run": True},
        )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "STEM_MANIFEST_PATH_NOT_FOUND"
    assert "manifest_path" in detail["message"].lower()
    assert "advtest" in detail["message"]


@pytest.mark.requirement("STEM-33")
def test_push_missing_without_external_roots_returns_503(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MDT_EXTERNAL_STEM_ROOTS", raising=False)
    with _assets_client(data_dir=tmp_path / "data") as client:
        resp = client.post("/api/v1/stems/push-missing", json={})
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["code"] == "MDT_EXTERNAL_STEM_ROOTS_MISSING"
    assert "MDT_EXTERNAL_STEM_ROOTS" in detail["message"]


@pytest.mark.requirement("STEM-33")
def test_hydrate_valid_manifest_dry_run_returns_200(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "files": {"vocals": "vocals.wav", "instrumental": "instrumental.wav"},
                "files_sha256": {"vocals": "a" * 64, "instrumental": "b" * 64},
            }
        ),
        encoding="utf-8",
    )
    with _assets_client(data_dir=tmp_path / "data") as client:
        resp = client.post(
            "/api/v1/stems/sid1/hydrate",
            json={"manifest_path": str(manifest), "dry_run": True},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
