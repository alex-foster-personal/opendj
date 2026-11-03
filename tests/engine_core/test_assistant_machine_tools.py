"""Machine routes and tools, end to end over a real socket.

The webui app is built by ``create_app`` with the two new routers included
under ``/api/v1`` exactly as the integrator will, served by uvicorn on an
ephemeral port. The routes are hit with httpx, and the tools run against
the same base URL. The volume round trips change the real OS volume and
restore the original in ``finally``.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from apps.engine_core.assistant.agent_contract import ToolContext, ToolFailure
from apps.engine_core.assistant.machine_tools import MACHINE_TOOLS
from apps.feature_flags import load_flags
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes import system_audio, usb_eject
from apps.webui.server.routes import usb_volumes as usb_mod
from tests.waits import start_uvicorn_in_thread

VOLUME_URL = "/api/v1/system/audio/volume"
TOOLS = {spec.name: spec for spec in MACHINE_TOOLS}


def _volume_arm_present() -> bool:
    if sys.platform == "win32":
        return True
    if sys.platform == "darwin":
        return Path("/usr/bin/osascript").is_file()
    return bool(shutil.which("wpctl") or shutil.which("pactl"))


def _usb_discovery_available() -> bool:
    return sys.platform == "darwin" and Path("/Volumes").is_dir() and Path("/usr/sbin/diskutil").is_file()


needs_volume_arm = pytest.mark.skipif(
    not _volume_arm_present(),
    reason="needs a mixer arm: Windows Core Audio, macOS /usr/bin/osascript, or Linux wpctl/pactl",
)


@pytest.fixture(scope="module")
def base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    usb_mod._reset_state_for_tests()
    data_dir = tmp_path_factory.mktemp("machine") / "data"
    (data_dir / "state").mkdir(parents=True)
    app = create_app(backend=InMemoryBackend(), bind_host="127.0.0.1", hostname="test-host", mount_frontend=False)
    app.state.data_dir = data_dir
    app.state.feature_flags = load_flags(data_dir)
    app.include_router(system_audio.router, prefix="/api/v1")
    app.include_router(usb_eject.router, prefix="/api/v1")
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="the machine-tools webui")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15.0)
        usb_mod._reset_state_for_tests()


def _run_tool(base: str, name: str, args: dict[str, Any] | None = None) -> Any:
    async def call() -> Any:
        return await TOOLS[name].run(ToolContext(base_url=base, audible=False), args or {})

    return asyncio.run(call())


def _tool_failure(base: str, name: str, args: dict[str, Any] | None = None) -> ToolFailure:
    with pytest.raises(ToolFailure) as caught:
        _run_tool(base, name, args)
    return caught.value


def _volume_or_skip(base: str) -> dict[str, Any]:
    """A host can carry a mixer binary with no audio server behind it, as a CI runner does."""
    response = httpx.get(f"{base}{VOLUME_URL}", timeout=60)
    if response.status_code == 503:
        pytest.skip(f"UNAVAILABLE: the mixer arm cannot read a volume here: {response.text}")
    assert response.status_code == 200, response.text
    return response.json()


def _other_percent(current: int) -> int:
    return current + 7 if current <= 90 else current - 7


# ----- contract ---------------------------------------------------------------


def test_tool_specs_name_and_gate() -> None:
    assert {s.name: s.gate for s in MACHINE_TOOLS} == {
        "system_volume_get": "read",
        "system_volume_set": "confirm",
        "usb_list": "read",
        "usb_eject": "confirm",
    }
    for spec in MACHINE_TOOLS:
        assert spec.parameters["type"] == "object"
        assert spec.description


def test_routes_are_in_the_live_openapi(base_url: str) -> None:
    paths = httpx.get(f"{base_url}/openapi.json", timeout=30).json()["paths"]
    assert set(paths[VOLUME_URL]) >= {"get", "put"}
    assert "post" in paths["/api/v1/usb/eject"]


# ----- volume route + tools ------------------------------------------------


@needs_volume_arm
def test_volume_route_round_trip_restores_original(base_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=60) as client:
        original = _volume_or_skip(base_url)
        target = _other_percent(original["volume"])
        try:
            put = client.put(VOLUME_URL, json={"volume": target})
            assert put.status_code == 200, put.text
            assert put.json()["volume"] == target
            assert client.get(VOLUME_URL).json()["volume"] == target
        finally:
            restored = client.put(VOLUME_URL, json={"volume": original["volume"]})
        assert restored.json()["volume"] == original["volume"]


@needs_volume_arm
def test_volume_tools_round_trip_restores_original(base_url: str) -> None:
    _volume_or_skip(base_url)
    original = _run_tool(base_url, "system_volume_get")
    assert set(original) == {"volume", "muted", "backend"}
    target = _other_percent(original["volume"])
    try:
        assert _run_tool(base_url, "system_volume_set", {"volume": target})["volume"] == target
        assert _run_tool(base_url, "system_volume_get")["volume"] == target
    finally:
        restored = _run_tool(base_url, "system_volume_set", {"volume": original["volume"]})
    assert restored["volume"] == original["volume"]


@pytest.mark.parametrize("bad", [101, -1, "50", 50.5, None, True])
def test_volume_put_rejects_non_percent_without_touching_mixer(base_url: str, bad: object) -> None:
    assert httpx.put(f"{base_url}{VOLUME_URL}", json={"volume": bad}, timeout=30).status_code == 422


@pytest.mark.parametrize("bad", [{}, {"volume": 101}, {"volume": -1}, {"volume": "50"}, {"volume": 50.5}, {"volume": True}])
def test_volume_set_tool_refuses_bad_arguments_locally(bad: dict[str, Any]) -> None:
    assert _tool_failure("http://127.0.0.1:9", "system_volume_set", bad).code == "invalid_arguments"


# ----- USB routes + tools ---------------------------------------------------


@pytest.mark.skipif(_usb_discovery_available(), reason="the refusal path; this Mac can discover volumes")
def test_usb_tools_surface_discovery_refusal(base_url: str) -> None:
    assert _tool_failure(base_url, "usb_list").code == "usb_volume_discovery_unavailable"
    assert _tool_failure(base_url, "usb_eject", {"volume_id": "vol:ANY"}).code == "usb_volume_discovery_unavailable"


@pytest.mark.skipif(not _usb_discovery_available(), reason="needs macOS /Volumes and /usr/sbin/diskutil")
def test_eject_tool_refuses_unknown_id_on_a_real_scan(base_url: str) -> None:
    assert _tool_failure(base_url, "usb_eject", {"volume_id": "vol:NO-SUCH-VOLUME"}).code == "unknown_volume"


@pytest.fixture
def stub_usb_engine() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Stands in for the engine's USB routes: this test is about the tool's two calls."""
    posted: list[dict[str, Any]] = []
    app = FastAPI()

    @app.get("/api/v1/usb/volumes")
    def volumes() -> dict[str, Any]:
        return {
            "volumes": [
                {"id": "vol:STICK", "mount_path": "/Volumes/STICK"},
                {"id": "vol:UNMOUNTED", "mount_path": None},
            ]
        }

    @app.post("/api/v1/usb/eject")
    def eject(body: dict[str, Any]) -> dict[str, Any]:
        posted.append(body)
        return {"mount_path": body["mount_path"], "name": "STICK", "ejected": True, "message": "ejected"}

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server, thread = start_uvicorn_in_thread(config, what="the stub USB engine")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", posted
    finally:
        server.should_exit = True
        thread.join(timeout=15.0)


def test_usb_eject_tool_sends_the_listed_mount_path_with_the_id(
    stub_usb_engine: tuple[str, list[dict[str, Any]]],
) -> None:
    base, posted = stub_usb_engine
    assert _run_tool(base, "usb_eject", {"volume_id": "vol:STICK"})["ejected"] is True
    assert posted == [{"mount_path": "/Volumes/STICK", "volume_id": "vol:STICK"}]


def test_usb_eject_tool_posts_nothing_for_an_unknown_or_unmounted_volume(
    stub_usb_engine: tuple[str, list[dict[str, Any]]],
) -> None:
    base, posted = stub_usb_engine
    assert _tool_failure(base, "usb_eject", {"volume_id": "vol:GONE"}).code == "unknown_volume"
    assert _tool_failure(base, "usb_eject", {"volume_id": "vol:UNMOUNTED"}).code == "volume_not_mounted"
    assert posted == []


@pytest.mark.parametrize("bad", [{}, {"volume_id": ""}, {"volume_id": 7}])
def test_usb_eject_tool_refuses_bad_arguments(base_url: str, bad: dict[str, Any]) -> None:
    assert _tool_failure(base_url, "usb_eject", bad).code == "invalid_arguments"


def test_tools_report_an_unreachable_engine() -> None:
    assert _tool_failure("http://127.0.0.1:9", "system_volume_get").code == "engine_unreachable"
