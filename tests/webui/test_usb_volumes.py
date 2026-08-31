"""USB volume tracker: pure classify + stub HTTP (#328).

Simulation hardening (ported from PR #412, adapted to the env gate): the
simulated-volume POST mounts only when MDT_USB_SIMULATION=1, simulated rows
never leak into an ungated app, and discovery fails explicitly (503) instead
of fail-soft empty lists.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes import usb_volumes as usb_mod
from apps.webui.server.routes.usb_volumes import (
    classify_mount,
    classify_role,
    hide_reason_for,
)


@pytest.fixture(autouse=True)
def _reset_usb_state() -> None:
    usb_mod._reset_state_for_tests()


def _make_app(*, simulation: bool, monkeypatch: pytest.MonkeyPatch):
    if simulation:
        monkeypatch.setenv("MDT_USB_SIMULATION", "1")
    else:
        monkeypatch.delenv("MDT_USB_SIMULATION", raising=False)
    return create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    app = _make_app(simulation=True, monkeypatch=monkeypatch)
    app.state.data_dir = data_dir
    with TestClient(app) as c:
        yield c


# ----- classify -----------------------------------------------------------


def test_classify_rekordbox_pioneer(tmp_path: Path) -> None:
    root = tmp_path / "STICK"
    (root / "PIONEER").mkdir(parents=True)
    assert classify_mount(root) == "rekordbox"


def test_classify_rekordbox_folder(tmp_path: Path) -> None:
    root = tmp_path / "STICK"
    (root / "rekordbox").mkdir(parents=True)
    assert classify_mount(root) == "rekordbox"


def test_classify_djay(tmp_path: Path) -> None:
    root = tmp_path / "STICK"
    (root / "djay").mkdir(parents=True)
    assert classify_mount(root) == "djay"


def test_classify_music_audio_file(tmp_path: Path) -> None:
    root = tmp_path / "STICK"
    root.mkdir()
    (root / "track.mp3").write_bytes(b"x")
    assert classify_mount(root) == "music"


def test_classify_music_nested_one_level(tmp_path: Path) -> None:
    root = tmp_path / "STICK"
    (root / "Music").mkdir(parents=True)
    (root / "Music" / "song.flac").write_bytes(b"x")
    assert classify_mount(root) == "music"


def test_classify_unknown_empty(tmp_path: Path) -> None:
    root = tmp_path / "STICK"
    root.mkdir()
    (root / "readme.txt").write_text("hi", encoding="utf-8")
    assert classify_mount(root) == "unknown"


def test_classify_missing_path(tmp_path: Path) -> None:
    assert classify_mount(tmp_path / "nope") == "unknown"


def test_skip_system_volume_names() -> None:
    assert usb_mod._skip_volume_name("Macintosh HD") is True
    assert usb_mod._skip_volume_name(".timemachine") is True
    assert usb_mod._skip_volume_name("com.apple.TimeMachine.localsnapshots") is True
    assert usb_mod._skip_volume_name("MYUSB") is False


def test_classify_role_usb_removable_vs_fixed() -> None:
    assert (
        classify_role(protocol="USB", removable=True, internal=False) == "usb_stick"
    )
    assert (
        classify_role(protocol="USB", removable=False, internal=False)
        == "mounted_drive"
    )
    assert classify_role(protocol="Disk Image", removable=True) == "disk_image"


def test_hide_reason_tags() -> None:
    assert (
        hide_reason_for("mounted_drive", protocol="USB")
        == "not-usb(mounted drive - USB)"
    )
    assert (
        hide_reason_for("disk_image", name="GitHub Copilot")
        == "not-usb(disk image - copilot)"
    )
    assert hide_reason_for("usb_stick") is None


# ----- simulation gate ----------------------------------------------------


@pytest.mark.parametrize("raw", ["", "0"])
def test_simulation_gate_off_values(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MDT_USB_SIMULATION", raw)
    assert usb_mod.simulation_enabled() is False


def test_simulation_gate_rejects_typos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDT_USB_SIMULATION", "true")
    with pytest.raises(ValueError, match="MDT_USB_SIMULATION"):
        usb_mod.simulation_enabled()


def test_gated_app_registers_simulation_and_read_only_usb_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _make_app(simulation=True, monkeypatch=monkeypatch).openapi()["paths"]

    assert "/api/v1/usb/volumes" in paths
    assert {"get", "post"} <= paths["/api/v1/usb/volumes"].keys()
    assert "/api/v1/usb/volumes/events" in paths


def test_production_app_excludes_usb_simulation_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_app(simulation=False, monkeypatch=monkeypatch)
    paths = app.openapi()["paths"]

    assert set(paths["/api/v1/usb/volumes"]) == {"get"}
    assert "/api/v1/usb/volumes/events" in paths
    with TestClient(app) as production_client:
        response = production_client.post(
            "/api/v1/usb/volumes",
            json={"name": "UNSAFE PRODUCTION SIMULATION"},
        )
    assert response.status_code == 405


# ----- HTTP ---------------------------------------------------------------


def test_get_volumes_ok(client: TestClient) -> None:
    r = client.get("/api/v1/usb/volumes")
    discovery_available = (
        sys.platform == "darwin"
        and Path("/Volumes").is_dir()
        and shutil.which("diskutil") is not None
    )
    if not discovery_available:
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "usb_volume_discovery_unavailable"
        assert r.json()["detail"]["reason"]
        return
    assert r.status_code == 200
    body = r.json()
    assert "volumes" in body
    assert isinstance(body["volumes"], list)
    assert body["watching"] is True


def test_post_simulate_volume(client: TestClient) -> None:
    r = client.post(
        "/api/v1/usb/volumes",
        json={"name": "FAKE STICK", "kind": "music", "mount_path": "/Volumes/FAKE"},
    )
    assert r.status_code == 200
    body = r.json()
    ids = {v["id"] for v in body["volumes"]}
    assert any(v["name"] == "FAKE STICK" and v["simulated"] for v in body["volumes"])
    assert any(i.startswith("sim:") for i in ids)
    fake = next(v for v in body["volumes"] if v["simulated"])
    assert fake["is_music"] is True
    assert fake["kind"] == "music"
    assert fake["role"] == "usb_stick"


def test_usb_event_stream_is_declared_in_live_openapi(client: TestClient) -> None:
    response = client.get("/openapi.json").json()["paths"][
        "/api/v1/usb/volumes/events"
    ]["get"]["responses"]["200"]

    assert set(response["content"]) == {"text/event-stream"}


def test_simulated_volume_state_has_a_hard_capacity(client: TestClient) -> None:
    for index in range(usb_mod._MAX_SIMULATED_VOLUMES):
        response = client.post(
            "/api/v1/usb/volumes",
            json={"id": f"sim:{index}", "name": f"Fixture {index}"},
        )
        assert response.status_code == 200

    overflow = client.post(
        "/api/v1/usb/volumes",
        json={"id": "sim:overflow", "name": "Overflow"},
    )

    assert overflow.status_code == 409
    assert overflow.json()["detail"] == {
        "code": "usb_simulation_capacity_exceeded",
        "reason": "simulated_volume_limit_reached",
    }


@pytest.mark.parametrize(
    "unsafe_id",
    ["vol:real-volume", "path:real-volume", "real-volume", "sim:"],
)
def test_simulated_volume_ids_require_reserved_namespace(
    client: TestClient,
    unsafe_id: str,
) -> None:
    response = client.post(
        "/api/v1/usb/volumes",
        json={"id": unsafe_id, "name": "Namespace adversary"},
    )

    assert response.status_code == 422


def test_simulation_rejects_an_adversarial_real_id_collision(
    client: TestClient,
) -> None:
    usb_mod._cached = [
        usb_mod.UsbVolume(
            id="sim:collision",
            name="Real volume",
            mount_path="/Volumes/REAL",
            kind="rekordbox",
            role="usb_stick",
        )
    ]

    response = client.post(
        "/api/v1/usb/volumes",
        json={"id": "sim:collision", "name": "Fake collision"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "usb_simulation_id_collision",
        "reason": "real_volume_id_reserved",
    }


def test_real_volumes_are_authoritative_during_adversarial_merge() -> None:
    real = usb_mod.UsbVolume(
        id="vol:authoritative",
        name="Real volume",
        mount_path="/Volumes/REAL",
        kind="rekordbox",
        role="usb_stick",
    )
    fake = usb_mod.UsbVolume(
        id=real.id,
        name="Fake collision",
        mount_path="/Volumes/FAKE",
        kind="music",
        simulated=True,
        role="usb_stick",
    )
    with usb_mod._FAKES_LOCK:
        usb_mod._fakes[fake.id] = fake

    assert usb_mod._with_simulations([real]) == [real]


def test_simulated_state_never_leaks_into_a_real_scan(
    client: TestClient,
    tmp_path: Path,
) -> None:
    injected = client.post(
        "/api/v1/usb/volumes",
        json={"id": "sim:isolated", "name": "Isolated"},
    )
    assert injected.status_code == 200

    empty_root = tmp_path / "Volumes"
    empty_root.mkdir()
    discovery = usb_mod.UsbDiscovery(
        volumes_root=empty_root,
        diskutil_command="/usr/bin/diskutil",
    )

    assert usb_mod._scan_volumes(force=True, discovery=discovery) == []
    test_scan = usb_mod._scan_volumes(
        force=True,
        discovery=discovery,
        include_simulated=True,
    )
    assert [volume.id for volume in test_scan] == ["sim:isolated"]


def test_discovery_contract_distinguishes_unsupported_from_no_devices(
    tmp_path: Path,
) -> None:
    with pytest.raises(usb_mod.UsbDiscoveryUnavailable) as unsupported:
        usb_mod._resolve_discovery(
            platform_name="linux",
            volumes_root=tmp_path,
            diskutil_command="/usr/bin/diskutil",
        )
    assert unsupported.value.reason == "unsupported_platform:linux"

    with pytest.raises(usb_mod.UsbDiscoveryUnavailable) as missing_diskutil:
        usb_mod._resolve_discovery(
            platform_name="darwin",
            volumes_root=tmp_path,
            diskutil_command=None,
        )
    assert missing_diskutil.value.reason == "diskutil_unavailable"

    empty_root = tmp_path / "Volumes"
    empty_root.mkdir()
    discovery = usb_mod._resolve_discovery(
        platform_name="darwin",
        volumes_root=empty_root,
        diskutil_command="/usr/bin/diskutil",
    )
    assert usb_mod._scan_volumes(force=True, discovery=discovery) == []
