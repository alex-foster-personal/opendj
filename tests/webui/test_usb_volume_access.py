"""USB volume root access is typed and never stalls the drive list (USBPLAY-02).

Requirements:

✔︎ Each volume root is listed under one shared bound; GET /usb/volumes answers
  within 1 s even while a listing hangs (the macOS permission prompt is open).
✔︎ access is "ok" | "pending" | "denied" | "unknown"; "unknown" is left only
  where no listing ran (simulated rows) or it failed for a non-permission reason.
✔︎ A pending or denied USB volume stays listed as a usb_stick, shown as music
  so the panel keeps it, never folded away as "not a USB stick".
✔︎ At most one listing per mount is in flight across scans.

Acceptance tests (single-line intent):

  - if a hung root listing holds GET /usb/volumes past 1 s then broken
  - if two hung listings cost two timeouts instead of one then broken
  - if a denied Fixed USB SSD folds away as a mounted drive then broken
  - if an unreadable root turns a readable Fixed USB backup into a stick then broken
  - if a second scan starts another listing while one is parked then broken

The OS boundary is ``os.listdir``: it is replaced by a fake that parks, refuses
or delegates to the real call, per path. Everything above it is real.

-Claude
"""

from __future__ import annotations

import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes import usb_root_access as access_mod
from apps.webui.server.routes import usb_volumes as usb_mod
from apps.webui.server.routes.usb_classify import classify_role

HTTP_BUDGET_S = 1.0
FIXED_USB = usb_mod.DiskutilInfo(volume_uuid=None, protocol="USB", removable=False, internal=False)


class FakeRootListing:
    """Stands in for ``os.listdir``: parks, refuses or fails chosen paths."""

    def __init__(self, real: Callable[..., list[str]]) -> None:
        self.real = real
        self.parked: dict[str, threading.Event] = {}
        self.denied: set[str] = set()
        self.failing: set[str] = set()
        self.calls: Counter[str] = Counter()
        self._lock = threading.Lock()

    def park(self, path: Path) -> threading.Event:
        return self.parked.setdefault(str(path), threading.Event())

    def __call__(self, path: str | os.PathLike[str] = ".") -> list[str]:
        key = os.fspath(path)
        with self._lock:
            self.calls[key] += 1
        release = self.parked.get(key)
        if release is not None:
            release.wait(timeout=10)
        if key in self.denied:
            raise PermissionError(1, "Operation not permitted", key)
        if key in self.failing:
            raise OSError(5, "Input/output error", key)
        return self.real(path)

    def release_all(self) -> None:
        for event in self.parked.values():
            event.set()


@pytest.fixture(autouse=True)
def _reset_usb_state() -> None:
    usb_mod._reset_state_for_tests()


@pytest.fixture
def roots(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeRootListing]:
    fake = FakeRootListing(os.listdir)
    monkeypatch.setattr(os, "listdir", fake)
    try:
        yield fake
    finally:
        fake.release_all()


def _volumes_root(tmp_path: Path, *names: str) -> Path:
    root = tmp_path / "Volumes"
    root.mkdir()
    for name in names:
        (root / name).mkdir()
    return root


def _discovery(volumes_root: Path) -> usb_mod.UsbDiscovery:
    return usb_mod.UsbDiscovery(volumes_root=volumes_root, diskutil_command="/usr/bin/diskutil")


def _scan(volumes_root: Path) -> tuple[dict[str, usb_mod.UsbVolumeOut], float]:
    start = time.monotonic()
    volumes = usb_mod._scan_volumes(force=True, discovery=_discovery(volumes_root))
    return {v.name: usb_mod._to_out(v) for v in volumes}, time.monotonic() - start


# ----- the listing itself ----------------------------------------------------------


def test_listing_types_ok_denied_and_other_failures(tmp_path: Path, roots: FakeRootListing) -> None:
    readable, refused, broken = (tmp_path / name for name in ("ok", "denied", "eio"))
    for path in (readable, refused, broken):
        path.mkdir()
    (readable / "PIONEER").mkdir()
    roots.denied.add(str(refused))
    roots.failing.add(str(broken))
    deadline = access_mod.root_listing_deadline()

    results = {
        path.name: access_mod.await_root_listing(
            access_mod.start_root_listing(path), deadline_mono=deadline
        )
        for path in (readable, refused, broken)
    }

    assert results["ok"] == access_mod.RootListing(access="ok", names=frozenset({"PIONEER"}))
    assert results["denied"] == access_mod.RootListing(access="denied"), (
        "if a refused listing is not typed denied then the permission state is invisible"
    )
    assert results["eio"] == access_mod.RootListing(access="unknown"), (
        "if an I/O failure reads as ok or denied then a failed measurement renders as a verdict"
    )


# ----- the scan ------------------------------------------------------------------------


def test_a_parked_listing_is_pending_and_the_scan_stays_bounded(
    tmp_path: Path, roots: FakeRootListing, monkeypatch: pytest.MonkeyPatch
) -> None:
    volumes_root = _volumes_root(tmp_path, "Parked", "Refused", "Stick", "Backup")
    (volumes_root / "Stick" / "PIONEER").mkdir()
    (volumes_root / "Backup" / "song.mp3").write_bytes(b"\0")
    roots.park(volumes_root / "Parked")
    roots.denied.add(str(volumes_root / "Refused"))
    monkeypatch.setattr(usb_mod, "_diskutil_info", lambda _mount, _cmd: FIXED_USB)

    by_name, elapsed = _scan(volumes_root)

    assert elapsed < HTTP_BUDGET_S, f"if a parked listing holds the scan {elapsed:.2f}s then broken"
    assert elapsed >= access_mod.ROOT_LISTING_TIMEOUT_S * 0.9, (
        f"control: the scan returned in {elapsed:.2f}s, so the parked listing was never awaited"
    )
    summary = {
        name: (out.access, out.role, out.kind, out.is_music, out.hide_reason)
        for name, out in by_name.items()
    }
    assert summary == {
        "Parked": ("pending", "usb_stick", "unknown", True, None),
        "Refused": ("denied", "usb_stick", "unknown", True, None),
        "Stick": ("ok", "usb_stick", "rekordbox", True, None),
        "Backup": ("ok", "mounted_drive", "unknown", False, "not-usb(mounted drive - USB)"),
    }, (
        "if an unreadable Fixed USB volume folds away, or a readable Fixed USB backup "
        f"is promoted to a stick, then USBPLAY-02 is broken: {summary}"
    )


def test_hung_listings_share_one_deadline(
    tmp_path: Path, roots: FakeRootListing, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = [f"Hung{i}" for i in range(4)]
    volumes_root = _volumes_root(tmp_path, *names)
    for name in names:
        roots.park(volumes_root / name)
    monkeypatch.setattr(usb_mod, "_diskutil_info", lambda _mount, _cmd: FIXED_USB)

    by_name, elapsed = _scan(volumes_root)

    assert {out.access for out in by_name.values()} == {"pending"}
    assert elapsed < HTTP_BUDGET_S, (
        f"if {len(names)} hung listings take {elapsed:.2f}s then each got its own timeout"
    )


def test_a_parked_listing_is_joined_not_restarted_and_clears_when_answered(
    tmp_path: Path, roots: FakeRootListing, monkeypatch: pytest.MonkeyPatch
) -> None:
    volumes_root = _volumes_root(tmp_path, "Stick")
    (volumes_root / "Stick" / "PIONEER").mkdir()
    stick = volumes_root / "Stick"
    prompt = roots.park(stick)
    monkeypatch.setattr(usb_mod, "_diskutil_info", lambda _mount, _cmd: FIXED_USB)

    first, _ = _scan(volumes_root)
    second, _ = _scan(volumes_root)

    assert (first["Stick"].access, second["Stick"].access) == ("pending", "pending")
    assert roots.calls[str(stick)] == 1, (
        f"if a second scan starts another listing ({roots.calls[str(stick)]} calls) "
        "then every scan parks one more thread behind the prompt"
    )
    assert access_mod._in_flight_mounts_for_tests() == [str(stick)]

    prompt.set()  # the user answers the prompt
    deadline = time.monotonic() + 5
    while access_mod._in_flight_mounts_for_tests() and time.monotonic() < deadline:
        time.sleep(0.01)
    answered, _ = _scan(volumes_root)

    assert (answered["Stick"].access, answered["Stick"].kind) == ("ok", "rekordbox"), (
        "if the stick stays pending after the prompt is answered then the listing never clears"
    )
    assert roots.calls[str(stick)] == 2, "a finished listing is not reused by a later scan"


# ----- over HTTP -------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.delenv("MDT_BUILD_PROFILE", raising=False)
    monkeypatch.delenv("MDT_FEATURE_FLAGS_FILE", raising=False)
    monkeypatch.setenv("MDT_USB_SIMULATION", "1")
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def test_get_volumes_answers_within_one_second_while_a_prompt_is_open(
    client: TestClient,
    tmp_path: Path,
    roots: FakeRootListing,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    volumes_root = _volumes_root(tmp_path, "Stick")
    roots.park(volumes_root / "Stick")
    # The host gates (darwin, /Volumes, diskutil, sandbox) are not under test here.
    monkeypatch.setattr(
        usb_mod, "_system_discovery", lambda *, usb_export_gate: _discovery(volumes_root)
    )
    monkeypatch.setattr(usb_mod, "_diskutil_info", lambda _mount, _cmd: FIXED_USB)
    simulated = client.post("/api/v1/usb/volumes", json={"id": "sim:access", "name": "Sim"})
    assert simulated.status_code == 200

    start = time.monotonic()
    response = client.get("/api/v1/usb/volumes")
    elapsed = time.monotonic() - start

    assert response.status_code == 200, response.text
    assert elapsed < HTTP_BUDGET_S, f"if GET /usb/volumes takes {elapsed:.2f}s then broken"
    rows = {row["name"]: row for row in response.json()["volumes"]}
    assert (rows["Stick"]["access"], rows["Stick"]["role"], rows["Stick"]["is_music"]) == (
        "pending",
        "usb_stick",
        True,
    ), "if the stick is missing or not marked pending then the prompt state is invisible"
    assert rows["Sim"]["access"] == "unknown", "no listing runs for a simulated row"


# ----- the role rule ------------------------------------------------------------------


def test_root_listed_only_changes_fixed_usb_volumes() -> None:
    def role(protocol: str, *, root_listed: bool) -> str:
        return classify_role(
            protocol=protocol,
            removable=False,
            has_dj_export=False,
            internal=False,
            root_listed=root_listed,
        )

    assert role("USB", root_listed=False) == "usb_stick"
    assert role("USB", root_listed=True) == "mounted_drive"
    assert role("Thunderbolt", root_listed=False) == "mounted_drive"
    assert role("Disk Image", root_listed=False) == "disk_image"
