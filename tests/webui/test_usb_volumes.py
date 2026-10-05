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

from apps.feature_flags import FlagRefusal, load_flags
from apps.feature_flags.profiles import BUILD_PROFILE_ENV, STORE_PROFILE
from apps.shared.sandbox import STORE_BUILD_REFUSAL_CODE, STORE_BUILD_REFUSAL_TITLE
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes import usb_volumes as usb_mod
from apps.webui.server.routes import usb_volumes_sim as sim_mod
from apps.webui.server.routes.usb_gate import UsbExportGate
from apps.webui.server.routes.usb_volumes import (
    classify_mount,
    classify_role,
    hide_reason_for,
)

_ENABLED_GATE = UsbExportGate(flag_enabled=True, refusal=None)
_DISABLED_LOCALLY_GATE = UsbExportGate(flag_enabled=False, refusal=None)
_DISABLED_BY_STORE_PROFILE_GATE = UsbExportGate(
    flag_enabled=False,
    refusal=FlagRefusal(
        code=STORE_BUILD_REFUSAL_CODE,
        message="usb.export is off in this build.",
        ui_title=STORE_BUILD_REFUSAL_TITLE,
    ),
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
    monkeypatch.delenv("MDT_BUILD_PROFILE", raising=False)
    monkeypatch.delenv("MDT_FEATURE_FLAGS_FILE", raising=False)
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    app = _make_app(simulation=True, monkeypatch=monkeypatch)
    app.state.data_dir = data_dir
    app.state.feature_flags = load_flags(data_dir)
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
        classify_role(
            protocol="USB", removable=True, has_dj_export=False, internal=False
        )
        == "usb_stick"
    )
    assert (
        classify_role(
            protocol="USB", removable=False, has_dj_export=False, internal=False
        )
        == "mounted_drive"
    )
    assert (
        classify_role(
            protocol="USB", removable=False, has_dj_export=True, internal=False
        )
        == "usb_stick"
    )
    assert (
        classify_role(protocol="Disk Image", removable=True, has_dj_export=True)
        == "disk_image"
    ), "if a disk image with PIONEER/ becomes a stick then the dmg fold is broken"
    assert (
        classify_role(
            protocol="Thunderbolt", removable=False, has_dj_export=True, internal=False
        )
        == "mounted_drive"
    ), "if a non-USB fixed drive becomes a stick then the promotion leaked off USB"


def test_music_kind_survives_missing_diskutil_metadata() -> None:
    """classify_role(protocol=None, removable=None) falls to "other"; when
    classify_mount() still proved music content, that must not be hidden
    behind a metadata gap that never asserted the volume is non-usb."""
    unclassifiable = usb_mod.UsbVolume(
        id="path:MYSTERY",
        name="MYSTERY",
        mount_path="/Volumes/MYSTERY",
        kind="music",
        role="other",
        protocol=None,
        removable=None,
    )
    out = usb_mod._to_out(unclassifiable)
    assert out.is_music is True
    assert out.hide_reason is None


def test_music_kind_stays_hidden_for_a_known_non_usb_role() -> None:
    """A role of "other" backed by an actual known protocol (just not one
    that maps to usb_stick/mounted_drive/disk_image) is a real non-usb bus,
    not a metadata gap, so it must stay hidden even if it contains audio."""
    known_other_bus = usb_mod.UsbVolume(
        id="path:THUNDER",
        name="THUNDER",
        mount_path="/Volumes/THUNDER",
        kind="music",
        role="other",
        protocol="Thunderbolt",
        removable=None,
    )
    out = usb_mod._to_out(known_other_bus)
    assert out.is_music is False


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
    assert sim_mod.simulation_enabled() is False


def test_simulation_gate_rejects_typos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDT_USB_SIMULATION", "true")
    with pytest.raises(ValueError, match="MDT_USB_SIMULATION"):
        sim_mod.simulation_enabled()


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
    for index in range(sim_mod._MAX_SIMULATED_VOLUMES):
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


def test_fixed_usb_drive_with_a_dj_export_is_shown_and_backup_drive_is_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A USB SSD reports RemovableMedia=false (Fixed), exactly like a USB
    backup disk. Live case Fri 25 Sep 2026: an SSK USB SSD holding a
    rekordbox export (PIONEER/rekordbox/export.pdb) was hidden as
    "not-usb(mounted drive - USB)". A DJ export at the root must promote it
    to a usb_stick; a fixed USB drive without one must stay hidden."""
    volumes_root = tmp_path / "Volumes"
    dj_ssd = volumes_root / "SSK Drive "
    (dj_ssd / "PIONEER" / "rekordbox").mkdir(parents=True)
    (dj_ssd / "PIONEER" / "rekordbox" / "export.pdb").write_bytes(b"\0")
    backup = volumes_root / "MaintainerBackup"
    (backup / "Backups.backupdb").mkdir(parents=True)
    (backup / "song.mp3").write_bytes(b"\0")
    fixed_usb = usb_mod.DiskutilInfo(protocol="USB", removable=False, internal=False)
    monkeypatch.setattr(usb_mod, "_diskutil_info", lambda _mount, _cmd: fixed_usb)
    discovery = usb_mod.UsbDiscovery(
        volumes_root=volumes_root,
        diskutil_command="/usr/bin/diskutil",
    )

    by_name = {
        v.name: usb_mod._to_out(v)
        for v in usb_mod._scan_volumes(force=True, discovery=discovery)
    }

    ssd = by_name["SSK Drive "]
    assert (ssd.role, ssd.kind, ssd.is_music, ssd.hide_reason) == (
        "usb_stick",
        "rekordbox",
        True,
        None,
    ), "if a fixed USB SSD with PIONEER/ is hidden then USB SSD export sticks are broken"
    kept_hidden = by_name["MaintainerBackup"]
    assert (kept_hidden.role, kept_hidden.kind, kept_hidden.is_music) == (
        "mounted_drive",
        "unknown",
        False,
    ), "if a fixed USB backup disk shows as music then the mounted-drive fold is broken"


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
            usb_export_gate=_ENABLED_GATE,
        )
    assert unsupported.value.reason == "unsupported_platform:linux"

    with pytest.raises(usb_mod.UsbDiscoveryUnavailable) as missing_diskutil:
        usb_mod._resolve_discovery(
            platform_name="darwin",
            volumes_root=tmp_path,
            diskutil_command=None,
            usb_export_gate=_ENABLED_GATE,
        )
    assert missing_diskutil.value.reason == "diskutil_unavailable"

    empty_root = tmp_path / "Volumes"
    empty_root.mkdir()
    discovery = usb_mod._resolve_discovery(
        platform_name="darwin",
        volumes_root=empty_root,
        diskutil_command="/usr/bin/diskutil",
        usb_export_gate=_ENABLED_GATE,
    )
    assert usb_mod._scan_volumes(force=True, discovery=discovery) == []


def test_a_disabled_flag_refuses_before_the_platform_check() -> None:
    """SAND-01/Thread-1, at the pure-function level: usb.export off must
    win over every other branch, so a developer testing the appstore
    profile on a non-darwin host sees the same refusal a real store build
    would, not "unsupported platform".
    """
    with pytest.raises(usb_mod.UsbDiscoveryUnavailable) as excinfo:
        usb_mod._resolve_discovery(
            platform_name="linux",
            volumes_root=Path("/nonexistent"),
            diskutil_command=None,
            usb_export_gate=_DISABLED_BY_STORE_PROFILE_GATE,
        )
    assert excinfo.value.reason == "usb_export_disabled_in_this_build"
    assert excinfo.value.ui_title == STORE_BUILD_REFUSAL_TITLE


def test_a_locally_disabled_flag_does_not_blame_the_sandbox() -> None:
    """SAND-01 review round 2 (Finding C): a flag off for a reason OTHER
    than the shipped store profile (a plain override, an explicit
    MDT_FEATURE_FLAGS_FILE) must still refuse, but must not claim Apple's
    sandbox forbids it -- that would blame the wrong party for a decision
    this machine made on its own.
    """
    with pytest.raises(usb_mod.UsbDiscoveryUnavailable) as excinfo:
        usb_mod._resolve_discovery(
            platform_name="linux",
            volumes_root=Path("/nonexistent"),
            diskutil_command=None,
            usb_export_gate=_DISABLED_LOCALLY_GATE,
        )
    assert excinfo.value.reason == "usb_export_disabled_in_this_build"
    assert excinfo.value.ui_title is None


# ----- SAND-01/Thread-1: the flag actually gates the HTTP route -----------
def test_appstore_profile_refuses_volume_listing_via_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The USB volumes route never read app.state.feature_flags, so the
    capability `/api/v1/flags` reports disabled stayed callable under the
    appstore profile. This drives the SAME flag store `/flags` reads.
    """
    monkeypatch.delenv("MDT_FEATURE_FLAGS_FILE", raising=False)
    monkeypatch.setenv(BUILD_PROFILE_ENV, STORE_PROFILE)
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    app = _make_app(simulation=True, monkeypatch=monkeypatch)
    app.state.data_dir = data_dir
    app.state.feature_flags = load_flags(data_dir)
    with TestClient(app) as appstore_client:
        response = appstore_client.get("/api/v1/usb/volumes")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "usb_volume_discovery_unavailable"
    assert detail["reason"] == "usb_export_disabled_in_this_build"
    assert detail["ui_title"] == STORE_BUILD_REFUSAL_TITLE


def test_a_local_override_refuses_volume_listing_without_blaming_the_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SAND-01 review round 2 (Finding C), at the HTTP level: a full-profile
    build with usb.export turned off via an explicit MDT_FEATURE_FLAGS_FILE
    (an operator's own choice, not the shipped store profile) must still
    refuse the route, but the refusal must not carry the App Store sandbox
    sentence -- that sentence is a lie about who made this decision.
    """
    monkeypatch.delenv(BUILD_PROFILE_ENV, raising=False)
    flags_file = tmp_path / "feature-flags.json"
    flags_file.write_text('{"usb.export": false}', encoding="utf-8")
    monkeypatch.setenv("MDT_FEATURE_FLAGS_FILE", str(flags_file))
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    app = _make_app(simulation=True, monkeypatch=monkeypatch)
    app.state.data_dir = data_dir
    app.state.feature_flags = load_flags(data_dir)
    with TestClient(app) as overridden_client:
        response = overridden_client.get("/api/v1/usb/volumes")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["reason"] == "usb_export_disabled_in_this_build"
    assert detail.get("ui_title") is None


def test_full_profile_volume_listing_is_not_refused_by_the_flag(
    client: TestClient,
) -> None:
    """The control: the default full-profile client (usb.export ON) must
    never carry this reason, even when discovery is unavailable for an
    unrelated reason (no darwin host in CI). If the gate keyed on anything
    but the resolved flag value, this would start failing for every
    developer running the plain build.
    """
    response = client.get("/api/v1/usb/volumes")
    if response.status_code == 503:
        assert response.json()["detail"]["reason"] != "usb_export_disabled_in_this_build"


pytestmark = pytest.mark.rb_parity
