"""Patch and profiles REST routes (issue #9888)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.lighting.http import PATCH_PATH_ATTR, PROFILES_DIR_ATTR, router


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    setattr(app.state, PATCH_PATH_ATTR, tmp_path / "patch.json")
    setattr(app.state, PROFILES_DIR_ATTR, tmp_path / "profiles")
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _fixture(fixture_id: str, address: int) -> dict[str, Any]:
    return {
        "id": fixture_id,
        "profile": "generic-rgb",
        "mode": "3ch",
        "universe": 1,
        "address": address,
        "groups": ["left"],
        "position": {"x": 0.5, "y": 0.5},
    }


def _store(*fixtures: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "active": "club", "patches": [{"name": "club", "fixtures": list(fixtures)}]}


@pytest.mark.requirement("LIGHT-14007")
def test_put_overlap_is_422_naming_both_ids_and_store_unchanged(tmp_path: Path) -> None:
    """[if] a PUT overlaps two fixtures [then] 422 names both ids and the stored patch is unchanged, [else stop]."""
    client = _client(tmp_path)
    good = _store(_fixture("par-a", 1))
    assert client.put("/api/v1/lighting/patch", json=good).status_code == 200
    response = client.put("/api/v1/lighting/patch", json=_store(_fixture("par-a", 1), _fixture("par-b", 2)))
    assert response.status_code == 422
    message = response.json()["detail"]["message"]
    assert "'par-a'" in message and "'par-b'" in message
    assert client.get("/api/v1/lighting/patch").json() == good


@pytest.mark.requirement("LIGHT-14007")
def test_profiles_lists_all_seven_generic_profiles(tmp_path: Path) -> None:
    """[if] GET /api/v1/lighting/profiles is read [then] all 7 generic profiles are listed, [else stop]."""
    rows = _client(tmp_path).get("/api/v1/lighting/profiles").json()
    generic = {r["id"] for r in rows if r["source"] == "builtin"}
    assert len(generic) == 7
    assert "generic-rgb" in generic
