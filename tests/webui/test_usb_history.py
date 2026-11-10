"""Vendor USB stick names merged onto live volume listings (#11166, #328)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox.paths import list_rekordbox_usb_mentions
from apps.feature_flags import load_flags
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes import usb_volumes as usb_mod
from apps.webui.server.routes.usb_history import load_vendor_usb_history, lookup_vendor_name

_STICK_UUID = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"


@pytest.fixture(autouse=True)
def _reset_usb_state() -> None:
    usb_mod._reset_state_for_tests()


def _write_rb_master(path: Path, *, device_id: str, label: str, volume_uuid: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE djmdDevice (
            ID VARCHAR(255),
            MasterDBID VARCHAR(255),
            Name VARCHAR(255),
            UUID VARCHAR(255),
            rb_local_deleted SMALLINT DEFAULT 0
        )
        """
    )
    conn.execute(
        "INSERT INTO djmdDevice (ID, MasterDBID, Name, UUID, rb_local_deleted) VALUES (?, ?, ?, ?, 0)",
        (device_id, "master-1", label, volume_uuid),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def data_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("MDT_BUILD_PROFILE", raising=False)
    monkeypatch.delenv("MDT_FEATURE_FLAGS_FILE", raising=False)
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
    )
    app.state.data_dir = data_dir
    app.state.feature_flags = load_flags(data_dir)
    app.state.usb_simulation_enabled = True
    with TestClient(app) as client:
        yield client


def test_uuid_match_supplies_rekordbox_vendor_name(tmp_path: Path) -> None:
    """[if] UUID matches rekordbox mention [then] stored name is returned [else stop]."""
    master = tmp_path / "master.plain.db"
    label = "Gig Stick Alpha"
    _write_rb_master(master, device_id="42", label=label, volume_uuid=_STICK_UUID)
    mentions = list_rekordbox_usb_mentions(master)
    assert len(mentions) == 1
    assert mentions[0].name == label

    history = load_vendor_usb_history(data_dir=tmp_path)
    hit = lookup_vendor_name(
        history,
        volume_id=f"vol:{_STICK_UUID}",
        volume_uuid=_STICK_UUID,
        mount_name="WRONG_LABEL",
    )
    assert hit is not None
    assert hit.name == label
    assert hit.source == "rekordbox"


def test_remount_at_new_mount_path_keeps_vol_uuid_identity(tmp_path: Path) -> None:
    """[if] same stick remounts at another /Volumes path [then] identity is vol:UUID [else stop]."""
    _write_rb_master(
        tmp_path / "master.plain.db",
        device_id="1",
        label="Named",
        volume_uuid=_STICK_UUID,
    )
    vol = usb_mod.UsbVolume(
        id=f"vol:{_STICK_UUID}",
        name="UNTITLED",
        mount_path="/Volumes/NEW_PATH",
        kind="music",
        role="usb_stick",
    )
    merged = usb_mod._apply_vendor_history([vol], data_dir=tmp_path)
    assert merged[0].id == f"vol:{_STICK_UUID}"
    assert merged[0].mount_path == "/Volumes/NEW_PATH"
    assert merged[0].name == "Named"
    assert merged[0].vendor_name == "Named"


def test_missing_vendor_db_still_lists_live_mounts(
    data_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] vendor DB is missing [then] listing still returns mounts [else stop]."""
    live = usb_mod.UsbVolume(
        id=f"vol:{_STICK_UUID}",
        name="LIVE",
        mount_path="/Volumes/LIVE",
        kind="music",
        role="usb_stick",
    )
    discovery = usb_mod.UsbDiscovery(
        volumes_root=Path("/Volumes"),
        diskutil_command="/usr/sbin/diskutil",
    )
    monkeypatch.setattr(usb_mod, "_system_discovery", lambda **_: discovery)
    monkeypatch.setattr(usb_mod, "_scan_volumes", lambda **_: [live])

    response = data_client.get("/api/v1/usb/volumes")
    assert response.status_code == 200
    body = response.json()
    assert len(body["volumes"]) == 1
    assert body["volumes"][0]["id"] == f"vol:{_STICK_UUID}"
    assert body["volumes"][0].get("vendor_name") is None


def test_get_volumes_merge_vendor_name_on_uuid(
    data_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] plain db names a UUID [then] GET merges vendor_name onto the row [else stop]."""
    data_dir = tmp_path / "data"
    label = "Rekordbox Gig Stick"
    _write_rb_master(
        data_dir / "master.plain.db",
        device_id="7",
        label=label,
        volume_uuid=_STICK_UUID,
    )
    live = usb_mod.UsbVolume(
        id=f"vol:{_STICK_UUID}",
        name="UNTITLED",
        mount_path="/Volumes/OTHER",
        kind="music",
        role="usb_stick",
    )
    discovery = usb_mod.UsbDiscovery(
        volumes_root=Path("/Volumes"),
        diskutil_command="/usr/sbin/diskutil",
    )
    monkeypatch.setattr(usb_mod, "_system_discovery", lambda **_: discovery)
    monkeypatch.setattr(usb_mod, "_scan_volumes", lambda **_: [live])

    response = data_client.get("/api/v1/usb/volumes")
    assert response.status_code == 200
    row = response.json()["volumes"][0]
    assert row["name"] == label
    assert row["vendor_name"] == label
    assert row["vendor_source"] == "rekordbox"


def test_load_vendor_history_swallows_unreadable_master(tmp_path: Path) -> None:
    """[if] rekordbox db is unreadable [then] history is empty without raising [else stop]."""
    bad = tmp_path / "master.plain.db"
    bad.write_bytes(b"not sqlite")
    history = load_vendor_usb_history(data_dir=tmp_path)
    assert history.by_uuid == {}
    assert history.by_device_id == {}
    assert history.by_mount_name == {}
