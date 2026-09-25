"""Play from USB routes: library, TrackOut, audio, artwork (USBPLAY-03/04/06).

The synthetic stick lives under a tmp ``/Volumes``; only the HOST facts
discovery asks the OS for are substituted (platform, the diskutil binary,
the volumes root, diskutil's per-volume answer), so the real gate, the real
``_scan_volumes`` and the real resolver run. The last section reads a REAL
stick, read-only, when ``MDT_USB_STICK_ROOT`` names its mount.

Regression intent, one line per guard:
  - if the library route does not return the locked JSON contract then the USBs tab breaks
  - if a stick route answers while usb.export is off then the store build leaks a capability
  - if a vanished stick answers anything but 404 USB_STICK_NOT_MOUNTED then the toast lies
  - if audio HEAD or Range stops working then deck load and seeking break on stick tracks
  - if a path outside the stick is streamed then containment is broken over HTTP
  - if any write method is routed under /usb/tracks then the stick is no longer read-only
  - if the real stick is not 563 tracks, 10 playlists, 759 entries in < 2 s then it regressed
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from apps.feature_flags import load_flags
from apps.feature_flags.profiles import BUILD_PROFILE_ENV, STORE_PROFILE
from apps.sync.usb import stick_library as sl
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes import usb_volumes as usb_mod
from tests.sync.usb.synthetic_stick import (
    ARTWORK_M_BYTES,
    ARTWORK_S_BYTES,
    AUDIO_BYTES,
    STICK_NAME,
    STICK_UUID,
    write_synthetic_stick,
)

API = "/api/v1/usb"
VOLUME_ID = f"vol:{STICK_UUID}"
NO_UUID_NAME = "NO UUID STICK"
LIBRARY_TRACK_KEYS = {
    "id", "pdb_id", "title", "artist", "album", "genre", "key", "bpm", "duration_s",
    "rating", "file_path", "has_analysis", "has_artwork", "date_added",
}


def _track_url(pdb_id: int, suffix: str = "") -> str:
    return f"{API}/tracks/usb-{STICK_UUID}-{pdb_id}{suffix}"


@pytest.fixture(autouse=True)
def _fresh_state() -> Iterator[None]:
    usb_mod._reset_state_for_tests()
    sl._reset_for_tests()
    yield
    usb_mod._reset_state_for_tests()
    sl._reset_for_tests()


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("MDT_USB_SIMULATION", raising=False)
    monkeypatch.delenv("MDT_FEATURE_FLAGS_FILE", raising=False)
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    app = create_app(
        backend=InMemoryBackend(), bind_host="127.0.0.1", hostname="test-host", mount_frontend=False
    )
    app.state.data_dir = data_dir
    app.state.feature_flags = load_flags(data_dir)
    return TestClient(app)


@pytest.fixture
def volumes_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A macOS-shaped host: the stick plus one volume diskutil gives no UUID."""
    root = tmp_path / "Volumes"
    write_synthetic_stick(root)
    (root / NO_UUID_NAME / "PIONEER").mkdir(parents=True)
    infos = {
        STICK_NAME: usb_mod.DiskutilInfo(
            volume_uuid=STICK_UUID, protocol="USB", removable=True, internal=False
        ),
        NO_UUID_NAME: usb_mod.DiskutilInfo(protocol="USB", removable=True, internal=False),
    }
    monkeypatch.delenv(BUILD_PROFILE_ENV, raising=False)
    monkeypatch.setattr(usb_mod, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(
        usb_mod, "shutil", SimpleNamespace(which=lambda name: f"/usr/sbin/{name}")
    )
    monkeypatch.setattr(usb_mod, "_VOLUMES_ROOT", root)
    monkeypatch.setattr(usb_mod, "_diskutil_info", lambda mount, _cmd: infos[mount.name])
    return root


@pytest.fixture
def mount(volumes_root: Path) -> Path:
    return volumes_root / STICK_NAME


@pytest.fixture
def client(
    volumes_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    with _client(tmp_path, monkeypatch) as test_client:
        yield test_client


def _code(response: object) -> str:
    return response.json()["detail"]["code"]  # type: ignore[attr-defined]


# ----- library --------------------------------------------------------------


def test_library_route_returns_the_locked_contract(client: TestClient, mount: Path) -> None:
    listed = client.get(f"{API}/volumes").json()["volumes"]
    assert VOLUME_ID in {v["id"] for v in listed}, "the stick's id comes from the volume list"
    body = client.get(f"{API}/volumes/{VOLUME_ID}/library").json()
    assert (body["volume_id"], body["volume_uuid"]) == (VOLUME_ID, STICK_UUID)
    assert body["name"] == STICK_NAME.strip() and body["mount_path"] == str(mount)
    assert body["counts"] == {
        "tracks": 9, "playlists": 5, "playlist_entries": 3, "history_playlists": 1
    }
    first = next(t for t in body["tracks"] if t["pdb_id"] == 1)
    assert set(first) == LIBRARY_TRACK_KEYS
    assert first == {
        "id": f"usb-{STICK_UUID}-1", "pdb_id": 1, "title": "First Synthetic",
        "artist": "Synth Artist", "album": "Synth Album", "genre": "Techno", "key": "Abm",
        "bpm": 124.5, "duration_s": 301.0, "rating": 4,
        "file_path": "/Contents/Synth Artist/First Synthetic .mp3",
        "has_analysis": True, "has_artwork": True, "date_added": "2026-09-01",
    }
    assert [p["id"] for p in body["playlists"]] == ["pl-5", "pl-7", "pl-6", "pl-3", "pl-9"]
    assert body["playlists"][3]["track_ids"] == [f"usb-{STICK_UUID}-1", f"usb-{STICK_UUID}-2"]
    assert body["history"] == [
        {"id": "hist-1", "name": "HISTORY 001",
         "track_ids": [f"usb-{STICK_UUID}-1", f"usb-{STICK_UUID}-2"]}
    ]
    assert isinstance(body["read_ms"], float) and body["cache_hit"] is False
    assert client.get(f"{API}/volumes/{VOLUME_ID}/library").json()["cache_hit"] is True


@pytest.mark.parametrize(
    "volume_id,status,code,reason",
    [
        (f"path:{NO_UUID_NAME}", 409, "USB_VOLUME_HAS_NO_UUID", "no_volume_uuid"),
        (f"vol:{STICK_UUID.lower()}", 409, "USB_VOLUME_HAS_NO_UUID", "volume_uuid_not_canonical"),
        ("SYN STICK", 422, "USB_VOLUME_ID_INVALID", None),
    ],
)
def test_library_route_refuses_ids_without_a_stable_uuid(
    client: TestClient, volume_id: str, status: int, code: str, reason: str | None
) -> None:
    response = client.get(f"{API}/volumes/{volume_id}/library")
    assert response.status_code == status
    assert _code(response) == code and response.json()["detail"].get("reason") == reason


def test_unmounted_stick_is_404_not_mounted_with_its_uuid(
    client: TestClient, mount: Path
) -> None:
    assert client.get(f"{API}/volumes/{VOLUME_ID}/library").status_code == 200
    mount.rename(mount.parent.parent / "unplugged")  # gone from /Volumes, like a pulled stick
    for url in (f"{API}/volumes/{VOLUME_ID}/library", _track_url(1), _track_url(1, "/audio")):
        response = client.get(url)
        assert response.status_code == 404, url
        assert response.json()["detail"]["code"] == "USB_STICK_NOT_MOUNTED"
        assert response.json()["detail"]["volume_uuid"] == STICK_UUID


def test_every_stick_route_answers_the_volume_lists_refusal_when_gated_off(
    volumes_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BUILD_PROFILE_ENV, STORE_PROFILE)
    with _client(tmp_path, monkeypatch) as store_client:
        listing = store_client.get(f"{API}/volumes")
        assert listing.status_code == 503
        for url in (
            f"{API}/volumes/{VOLUME_ID}/library",
            _track_url(1),
            _track_url(1, "/audio"),
            _track_url(1, "/artwork"),
        ):
            response = store_client.get(url)
            assert (response.status_code, response.json()) == (503, listing.json()), url
        assert store_client.head(_track_url(1, "/audio")).status_code == 503


# ----- one track ------------------------------------------------------------


def test_track_route_returns_trackout_with_stick_flags(client: TestClient, mount: Path) -> None:
    response = client.get(_track_url(1))
    assert response.status_code == 200 and response.headers["etag"].startswith('"')
    body = response.json()
    assert body["stable_id"] == f"usb-{STICK_UUID}-1"
    assert (body["title"], body["artist"], body["key"], body["bpm"]) == (
        "First Synthetic", "Synth Artist", "Abm", 124.5
    )
    assert (body["duration_ms"], body["rating"], body["created_at"]) == (301000, 4, "2026-09-01")
    assert body["file_path"] == str(mount.resolve() / "Contents/Synth Artist/First Synthetic .mp3")
    assert body["artwork_available"] is True
    assert not any(
        body[flag]
        for flag in ("has_rb_mapping", "lyrics_available", "auto_cues_available", "stems_available")
    )
    no_art = client.get(_track_url(2)).json()
    assert no_art["artwork_available"] is False and no_art["created_at"] == no_art["updated_at"]


def test_track_artwork_flag_is_false_when_the_m_file_is_missing(
    client: TestClient, mount: Path
) -> None:
    (mount / "PIONEER" / "Artwork" / "00001" / "a1_m.jpg").unlink()
    assert client.get(_track_url(1)).json()["artwork_available"] is False


@pytest.mark.parametrize(
    "track_id,status,code",
    [
        (f"usb:{STICK_UUID}-1", 422, "USB_TRACK_ID_INVALID"),
        (f"usb-{STICK_UUID}-01", 422, "USB_TRACK_ID_INVALID"),
        (f"usb-{STICK_UUID}-404", 404, "USB_TRACK_NOT_FOUND"),
    ],
)
def test_track_route_refusals(client: TestClient, track_id: str, status: int, code: str) -> None:
    for suffix in ("", "/audio", "/artwork"):
        response = client.get(f"{API}/tracks/{track_id}{suffix}")
        assert (response.status_code, _code(response)) == (status, code), suffix


# ----- audio ----------------------------------------------------------------


def test_audio_get_head_and_range(client: TestClient) -> None:
    full = client.get(_track_url(1, "/audio"))
    assert full.status_code == 200 and full.content == AUDIO_BYTES
    assert full.headers["content-type"] == "audio/mpeg"
    assert full.headers["cache-control"] == "no-store"
    assert (full.headers["x-audio-kind"], full.headers["x-audio-source"]) == ("local", "usb-stick")
    head = client.head(_track_url(1, "/audio"))
    assert head.status_code == 200 and head.content == b""
    assert int(head.headers["content-length"]) == len(AUDIO_BYTES)
    ranged = client.get(_track_url(1, "/audio"), headers={"Range": "bytes=2-5"})
    assert ranged.status_code == 206 and ranged.content == AUDIO_BYTES[2:6]
    assert ranged.headers["content-range"] == f"bytes 2-5/{len(AUDIO_BYTES)}"
    assert client.get(_track_url(2, "/audio")).headers["content-type"] == "audio/flac"


@pytest.mark.parametrize(
    "pdb_id,reason",
    [(4, "escapes_mount"), (5, "escapes_mount"), (3, "extension_not_allowed"), (9, "invalid_path")],
)
def test_audio_outside_the_stick_is_403(client: TestClient, pdb_id: int, reason: str) -> None:
    response = client.get(_track_url(pdb_id, "/audio"))
    assert response.status_code == 403
    assert (_code(response), response.json()["detail"]["reason"]) == (
        "USB_PATH_OUTSIDE_VOLUME", reason
    )


def test_missing_audio_file_is_404_file_missing(client: TestClient, mount: Path) -> None:
    (mount / "Contents" / "second.flac").unlink()
    response = client.get(_track_url(2, "/audio"))
    assert (response.status_code, _code(response)) == (404, "USB_FILE_MISSING")


def test_unreadable_audio_is_503_access_blocked(client: TestClient, mount: Path) -> None:
    audio = mount / "Contents" / "second.flac"
    audio.chmod(0)
    try:
        if os.access(audio, os.R_OK):
            pytest.skip("chmod 000 does not block this user (root)")
        response = client.get(_track_url(2, "/audio"))
    finally:
        audio.chmod(0o644)
    assert (response.status_code, _code(response)) == (503, "AUDIO_ACCESS_BLOCKED")


# ----- artwork --------------------------------------------------------------


@pytest.mark.parametrize(
    "size,expected", [("s", ARTWORK_S_BYTES), ("m", ARTWORK_M_BYTES), ("orig", ARTWORK_M_BYTES)]
)
def test_artwork_sizes(client: TestClient, size: str, expected: bytes) -> None:
    response = client.get(_track_url(1, "/artwork"), params={"size": size})
    assert response.status_code == 200 and response.content == expected
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-cache"


def test_artwork_refusals(client: TestClient) -> None:
    assert client.get(_track_url(1, "/artwork"), params={"size": "xl"}).status_code == 422
    outside = client.get(_track_url(6, "/artwork"))
    assert (outside.status_code, outside.json()["detail"]["reason"]) == (403, "outside_allowed_dir")
    none = client.get(_track_url(2, "/artwork"))
    assert (none.status_code, _code(none)) == (404, "USB_FILE_MISSING")


# ----- read only and documented ---------------------------------------------


def test_no_write_method_is_routed_under_usb_tracks(client: TestClient) -> None:
    stick_routes = [
        route for route in client.app.routes  # type: ignore[attr-defined]
        if isinstance(route, APIRoute) and route.path.startswith(f"{API}/tracks")
    ]
    # Positive control: the scan sees this lane's routes (lane A2 adds more,
    # and they are held to the same rule without editing this test).
    assert {f"{API}/tracks/{{track_id}}{s}" for s in ("", "/audio", "/artwork")} <= {
        route.path for route in stick_routes
    }
    assert set().union(*(route.methods for route in stick_routes)) <= {"GET", "HEAD"}
    for method in ("post", "put", "patch", "delete"):
        assert client.request(method, _track_url(1)).status_code == 405


def test_openapi_documents_every_stick_refusal(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    audio = paths[f"{API}/tracks/{{track_id}}/audio"]
    assert audio["get"]["operationId"] == (
        "get_usb_track_audio_api_v1_usb_tracks__track_id__audio_get"
    )
    assert audio["head"]["operationId"] == (
        "head_usb_track_audio_api_v1_usb_tracks__track_id__audio_head"
    )
    for path in (
        f"{API}/volumes/{{volume_id}}/library",
        f"{API}/tracks/{{track_id}}",
        f"{API}/tracks/{{track_id}}/audio",
        f"{API}/tracks/{{track_id}}/artwork",
    ):
        assert {"403", "404", "409", "422", "503"} <= set(paths[path]["get"]["responses"]), path


# ----- live: a real stick, read only -----------------------------------------

_LIVE_ENV = "MDT_USB_STICK_ROOT"
# The reference test stick's export (read Fri 25 Sep 2026). Another stick
# needs its own numbers; these are what USBPLAY-03 was measured against.
_LIVE_COUNTS = {"tracks": 563, "playlists": 10, "playlist_entries": 759}
_LIVE_COLD_BUDGET_MS = 2000.0


@pytest.fixture
def live_mount() -> Path:
    raw = os.environ.get(_LIVE_ENV, "")
    if not raw:
        reason = f"{_LIVE_ENV} is unset: no real rekordbox stick to read (set it to the mount path)"
        print(reason)
        pytest.skip(reason)
    root = Path(raw)
    if not root.joinpath(*sl.EXPORT_PDB_PARTS).is_file():
        pytest.fail(f"{_LIVE_ENV}={raw!r} is set but has no {'/'.join(sl.EXPORT_PDB_PARTS)}")
    return root


@pytest.fixture
def live_client(
    live_mount: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.delenv(BUILD_PROFILE_ENV, raising=False)
    with _client(tmp_path, monkeypatch) as test_client:
        yield test_client


def _live_volume_id(client: TestClient, live_mount: Path) -> str:
    listing = client.get(f"{API}/volumes")
    assert listing.status_code == 200, listing.text
    ids = [v["id"] for v in listing.json()["volumes"] if v["mount_path"] == str(live_mount)]
    assert len(ids) == 1, f"real discovery did not list {str(live_mount)!r} exactly once: {ids}"
    return ids[0]


def test_live_stick_library_counts_and_cold_budget(
    live_client: TestClient, live_mount: Path
) -> None:
    volume_id = _live_volume_id(live_client, live_mount)
    usb_mod._reset_state_for_tests()
    sl._reset_for_tests()
    started = time.perf_counter()
    cold = live_client.get(f"{API}/volumes/{volume_id}/library")
    cold_ms = (time.perf_counter() - started) * 1000.0
    started = time.perf_counter()
    warm = live_client.get(f"{API}/volumes/{volume_id}/library")
    warm_ms = (time.perf_counter() - started) * 1000.0
    assert cold.status_code == 200, cold.text
    body = cold.json()
    print(
        f"live library: cold {cold_ms:.1f} ms (server read_ms {body['read_ms']:.1f}), "
        f"warm {warm_ms:.1f} ms (server read_ms {warm.json()['read_ms']:.1f}), "
        f"counts {body['counts']}"
    )
    assert {k: body["counts"][k] for k in _LIVE_COUNTS} == _LIVE_COUNTS
    assert len(body["tracks"]) == _LIVE_COUNTS["tracks"]
    assert sum(len(p["track_ids"]) for p in body["playlists"]) == _LIVE_COUNTS["playlist_entries"]
    assert body["cache_hit"] is False and warm.json()["cache_hit"] is True
    assert cold_ms < _LIVE_COLD_BUDGET_MS


def test_live_stick_track_audio_and_artwork(live_client: TestClient, live_mount: Path) -> None:
    volume_id = _live_volume_id(live_client, live_mount)
    tracks = live_client.get(f"{API}/volumes/{volume_id}/library").json()["tracks"]
    playable = next(t for t in tracks if live_mount.joinpath(t["file_path"].lstrip("/")).is_file())
    track = live_client.get(f"{API}/tracks/{playable['id']}")
    assert track.status_code == 200 and track.json()["stable_id"] == playable["id"]
    audio_url = f"{API}/tracks/{playable['id']}/audio"
    head = live_client.head(audio_url)
    assert head.status_code == 200 and int(head.headers["content-length"]) > 0
    ranged = live_client.get(audio_url, headers={"Range": "bytes=0-1023"})
    assert ranged.status_code == 206 and len(ranged.content) == 1024
    with_art = next(t for t in tracks if t["has_artwork"])
    artwork = live_client.get(f"{API}/tracks/{with_art['id']}/artwork", params={"size": "m"})
    assert artwork.status_code == 200 and artwork.content[:2] == b"\xff\xd8"
