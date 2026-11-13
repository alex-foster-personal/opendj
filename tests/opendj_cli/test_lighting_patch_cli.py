"""``opendj lighting patch`` verbs over the patch REST routes (issue #9888)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.lighting.http import PATCH_PATH_ATTR, PROFILES_DIR_ATTR, router
from apps.opendj_cli import lighting_cli
from apps.opendj_cli.__main__ import main
from apps.opendj_cli.api_cli import _BackendTarget


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app = FastAPI()
    setattr(app.state, PATCH_PATH_ATTR, tmp_path / "patch.json")
    setattr(app.state, PROFILES_DIR_ATTR, tmp_path / "profiles")
    app.include_router(router, prefix="/api/v1")
    test_client = TestClient(app, base_url="http://127.0.0.1:9")
    monkeypatch.setattr(lighting_cli.httpx, "Client", lambda *a, **k: nullcontext(test_client))
    lock = tmp_path / "test.engine.lock"
    lock.write_text(json.dumps({"version": 1, "api_base_url": "http://127.0.0.1:9", "pid": 1, "role": "test"}))
    monkeypatch.setattr(
        lighting_cli,
        "resolve_backend_base_url",
        lambda environ=None, lock_path=None: _BackendTarget(base_url="http://127.0.0.1:9"),
    )
    yield test_client


@pytest.mark.requirement("LIGHT-14007")
def test_patch_add_then_get_lists_fixture(client: TestClient, tmp_path: Path) -> None:
    """[if] `opendj lighting patch add --profile generic-rgb ...` runs [then] GET patch lists the fixture, [else stop]."""
    lock = str(tmp_path / "test.engine.lock")
    argv = ["--lock", lock, "lighting", "patch", "add", "--profile", "generic-rgb"]
    assert main([*argv, "--universe", "1", "--address", "1", "--group", "left"]) == 0
    store = client.get("/api/v1/lighting/patch").json()
    fixtures = next(p for p in store["patches"] if p["name"] == store["active"])["fixtures"]
    assert [(f["profile"], f["universe"], f["address"], f["groups"]) for f in fixtures] == [
        ("generic-rgb", 1, 1, ["left"])
    ]
