"""PUT next-up prefetches the head's stem-set (STEM-11747).

[if] the head is cloud [then] the PUT stays 200 and GET stems/state reads local, [else stop].
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apps.webui.server.routes.stems as stems_module
from apps.cloud.stem_cache_settings import StemCacheSettings, save_settings
from apps.cloud.stem_hydration import HydrationOutcome, NextUpStemPrefetcher, PrefetchDeps
from apps.cloud.stem_index import save_cached_index
from apps.cloud.stem_source import STEM_HUB_UNREACHABLE, DirectR2Source, StemSourceError
from apps.webui.server.routes import ahead_analysis
from tests.cloudsync.conftest import InMemoryAssetS3
from tests.webui.test_stems_hydration import (
    _cfg,
    _reset_hydration_module_state,  # noqa: F401 - autouse fixture
    _seed_bundle,
)

pytestmark = pytest.mark.requirement("STEM-11747")
LOGGER = "apps.cloud.stem_hydration"


class _Drain:
    def set_next_up(self, stable_ids: list[str]) -> list[str]:
        return list(stable_ids)


class _FakeStems:
    def __init__(self, states: dict[str, str], *, floor: str | None = None, fail: set[str] | None = None) -> None:
        self.states, self.floor, self.fail = states, floor, fail or set()
        self.calls: list[str] = []

    def hydrate(self, sid: str) -> HydrationOutcome:
        self.calls.append(sid)
        if sid in self.fail:
            raise StemSourceError(STEM_HUB_UNREACHABLE, "hub unreachable: connection refused")
        self.states[sid] = "local"
        return HydrationOutcome(sid, "hydrated")

    def deps(self) -> PrefetchDeps:
        return PrefetchDeps(lambda sid: self.states.get(sid, "none"), lambda: self.floor, self.hydrate)


def _client(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeStems, submit: Callable[[Callable[[], None]], object] | None = None
) -> TestClient:
    app = FastAPI()
    app.include_router(ahead_analysis.router, prefix="/api/v1")
    app.state.ahead_analysis = _Drain()
    app.state.stem_next_up_prefetch = NextUpStemPrefetcher(submit=submit or (lambda fn: fn()))
    monkeypatch.setattr(ahead_analysis, "stem_prefetch_deps", lambda request: fake.deps())
    return TestClient(app)


def _put(client: TestClient, ids: list[str]) -> Any:
    response = client.put("/api/v1/ahead-analysis/next-up", json={"stable_ids": ids})
    assert response.status_code == 200 and response.json() == {"ok": True, "next_up": ids}
    return response


def test_next_up_cloud_head_is_hydrated_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeStems({"X": "cloud"})
    client = _client(monkeypatch, fake)
    _put(client, ["X", "Q"])
    _put(client, ["X"])
    assert fake.calls == ["X"], "if the same head is hydrated twice then broken"


def test_next_up_new_head_queues_behind_the_one_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, queued = _FakeStems({"X": "cloud", "Y": "cloud"}), []
    client = _client(monkeypatch, fake, submit=queued.append)
    _put(client, ["X"])
    _put(client, ["Y"])
    assert len(queued) == 1, "if a changed head starts a second prefetch then broken"
    while queued:
        queued.pop(0)()
    assert fake.calls == ["X", "Y"]


@pytest.mark.parametrize("state", ["local", "none"])
def test_next_up_local_or_unindexed_head_is_not_hydrated(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    fake = _FakeStems({"X": state})
    _put(_client(monkeypatch, fake), ["X"])
    assert fake.calls == []


def test_next_up_hub_error_keeps_the_put_200_and_logs_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakeStems({"X": "cloud", "Y": "cloud"}, fail={"X"})
    client = _client(monkeypatch, fake)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _put(client, ["X"])
        _put(client, ["Y"])
    assert [r.levelno for r in caplog.records].count(logging.WARNING) == 1
    assert fake.calls == ["X", "Y"] and fake.states["Y"] == "local", "if the next head is not tried then broken"


def test_next_up_below_cache_floor_skips_with_a_named_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _FakeStems({"X": "cloud"}, floor="free 1 bytes < floor 2 bytes")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _put(_client(monkeypatch, fake), ["X"])
    assert fake.calls == []
    assert any("below_cache_floor" in r.getMessage() for r in caplog.records)


def test_next_up_without_a_prefetcher_is_unchanged() -> None:
    app = FastAPI()
    app.include_router(ahead_analysis.router, prefix="/api/v1")
    app.state.ahead_analysis = _Drain()
    _put(TestClient(app), ["X"])


def test_next_up_prefetch_reads_local_on_the_state_route(tmp_path: Path) -> None:
    """CLI/HTTP parity: the real stems routes over an in-memory R2 fake."""
    sid = "track-prefetch-1"
    stems_dir, data_dir = tmp_path / "stems", tmp_path / "data"
    stems_dir.mkdir()
    data_dir.mkdir()
    cfg, s3 = _cfg(), InMemoryAssetS3()
    save_cached_index(data_dir, {sid: _seed_bundle(s3, cfg, sid)})
    save_settings(data_dir, StemCacheSettings(floor_gib=0, floor_fraction=0))
    app = FastAPI()
    app.state.stems_dir = stems_dir
    app.state.stem_hydration_data_dir = data_dir
    app.state.stem_hydration_source = DirectR2Source(cfg=cfg, s3=s3)
    app.state.stem_hydration_cfg = cfg
    app.state.stem_hydration_s3 = s3
    app.state.ahead_analysis = _Drain()
    prefetcher = NextUpStemPrefetcher(submit=lambda fn: fn())
    app.state.stem_next_up_prefetch = prefetcher
    app.include_router(ahead_analysis.router, prefix="/api/v1")
    app.include_router(stems_module.router, prefix="/api/v1")
    client = TestClient(app)

    state_url = f"/api/v1/tracks/{sid}/stems/state"
    assert client.get(state_url).json()["state"] == "cloud"
    _put(client, [sid])
    assert prefetcher.history[-1].status == "hydrated", prefetcher.history[-1]
    body = client.get(state_url).json()
    assert body["state"] == "local", "if the prefetched stem-set is not local before the load then broken"
    assert body["deck_open"] is False, "if a prefetch hands the stem-set to a deck then broken"
