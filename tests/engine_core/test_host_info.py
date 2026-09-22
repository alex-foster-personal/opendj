"""PERFMODE-01 host-info route and CLI."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.host_info import (
    CODE_HOST_INFO_UNAVAILABLE,
    HOST_INFO_PATH,
    HostInfoUnavailable,
    add_host_info_route,
    main,
    read_host_facts,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _client(data_dir: Path) -> Iterator[TestClient]:
    app = FastAPI()
    add_host_info_route(app, data_dir=data_dir)
    with TestClient(app) as client:
        yield client


@pytest.mark.requirement("PERFMODE-01")
def test_read_host_facts_returns_positive_ints(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    [if] psutil reports cpu count and ram [then] read_host_facts returns them as ints, [else stop].
    """
    psutil = MagicMock()
    psutil.cpu_count.return_value = 8
    psutil.virtual_memory.return_value = MagicMock(total=16 * 1024**3)
    monkeypatch.setitem(__import__("sys").modules, "psutil", psutil)
    logical, ram = read_host_facts()
    assert logical == 8
    assert ram == 16 * 1024**3


@pytest.mark.requirement("PERFMODE-01")
def test_cpu_count_none_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    [if] cpu_count is None [then] read_host_facts raises HostInfoUnavailable, [else stop].
    """
    psutil = MagicMock()
    psutil.cpu_count.return_value = None
    monkeypatch.setitem(__import__("sys").modules, "psutil", psutil)
    with pytest.raises(HostInfoUnavailable, match="None"):
        read_host_facts()


@pytest.mark.requirement("PERFMODE-01")
def test_virtual_memory_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    [if] virtual_memory raises [then] read_host_facts raises HostInfoUnavailable, [else stop].
    """
    psutil = MagicMock()
    psutil.cpu_count.return_value = 4
    psutil.virtual_memory.side_effect = OSError("nope")
    monkeypatch.setitem(__import__("sys").modules, "psutil", psutil)
    with pytest.raises(HostInfoUnavailable, match="virtual_memory"):
        read_host_facts()


@pytest.mark.requirement("PERFMODE-01")
def test_route_200_has_measured_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    [if] host facts are readable [then] the route returns 200 with measured fields, [else stop].
    """
    psutil = MagicMock()
    psutil.cpu_count.return_value = 10
    psutil.virtual_memory.return_value = MagicMock(total=32 * 1024**3)
    monkeypatch.setitem(__import__("sys").modules, "psutil", psutil)
    with TestClient(FastAPI()) as _:
        pass
    app = FastAPI()
    add_host_info_route(app, data_dir=tmp_path)
    client = TestClient(app)
    response = client.get(HOST_INFO_PATH)
    assert response.status_code == 200
    body = response.json()
    assert body["logical_cpus"] == 10
    assert body["ram_bytes"] == 32 * 1024**3
    assert body["canary_hashes_per_second"] is not None


@pytest.mark.requirement("PERFMODE-01")
def test_route_503_on_unreadable_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    [if] host facts are unreadable [then] the route returns 503 with the failure, [else stop].
    """
    psutil = MagicMock()
    psutil.cpu_count.return_value = None
    monkeypatch.setitem(__import__("sys").modules, "psutil", psutil)
    app = FastAPI()
    add_host_info_route(app, data_dir=tmp_path)
    client = TestClient(app)
    response = client.get(HOST_INFO_PATH)
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == CODE_HOST_INFO_UNAVAILABLE
    assert "None" in body["message"]


@pytest.mark.requirement("PERFMODE-01")
def test_failure_is_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    [if] the first host-info request fails [then] later requests stay 503, cached, [else stop].
    """
    psutil = MagicMock()
    psutil.cpu_count.return_value = None
    monkeypatch.setitem(__import__("sys").modules, "psutil", psutil)
    app = FastAPI()
    add_host_info_route(app, data_dir=tmp_path)
    client = TestClient(app)
    client.get(HOST_INFO_PATH)
    psutil.cpu_count.return_value = 8
    psutil.virtual_memory.return_value = MagicMock(total=8 * 1024**3)
    second = client.get(HOST_INFO_PATH)
    assert second.status_code == 503


@pytest.mark.requirement("PERFMODE-01")
def test_cli_main_exit_zero_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    [if] host facts are readable [then] the CLI main() exits 0 and prints them as json, [else stop].
    """
    psutil = MagicMock()
    psutil.cpu_count.return_value = 4
    psutil.virtual_memory.return_value = MagicMock(total=8 * 1024**3)
    monkeypatch.setitem(__import__("sys").modules, "psutil", psutil)
    monkeypatch.setattr("apps.engine_core.host_info.DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("apps.shared.paths.DATA_DIR", tmp_path)
    assert main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["logical_cpus"] == 4


@pytest.mark.requirement("PERFMODE-01")
def test_engine_registers_host_and_perf_tier_before_spa_mount() -> None:
    """
    [if] app.py is read [then] host-info and perf-tier register before the spa mount, [else stop].
    """
    source = (REPO_ROOT / "apps/engine_core/app.py").read_text(encoding="utf-8")
    spa = source.index("_mount_spa(app)")
    assert source.index("add_host_info_route(") < spa
    assert source.index("add_perf_tier_route(") < spa
    assert source.index("add_app_posture_route(") < spa
