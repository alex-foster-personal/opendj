"""USB volume tracker: pure classify + stub HTTP (#328)."""

from __future__ import annotations

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


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
    )
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


# ----- HTTP ---------------------------------------------------------------


def test_get_volumes_ok(client: TestClient) -> None:
    r = client.get("/api/v1/usb/volumes")
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
