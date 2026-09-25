"""Play from USB routes: library, TrackOut, audio, anlz, hot-cues, artwork (USBPLAY-03..08).

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
  - if /anlz is not the library AnlzData shape with a rekordbox grid and vocals then decks break
  - if two tracks sharing an ANLZ directory get one grid then a stick track plays the wrong cues
  - if /anlz points bounds, ETag or Cache-Control drift from the library route then caching lies
  - if /hot-cues disagrees with /anlz's hot cues then the deck's cue bank and waveform disagree
  - if an unanalyzed stick track 404s /anlz or /hot-cues then it cannot load onto a deck
  - if TrackOut flags predict a different answer than the routes give then the deck asks wrongly
  - if the listing's has_artwork disagrees with TrackOut's artwork_available then rows fetch 404s
  - if a playlist's track_ids are not the stick's entry_index order then USBPLAY-05 order is lost
  - if any file under PIONEER/ or Contents/ changes while the routes run then USBPLAY-08 broke
  - if a same-size, same-mtime content change under PIONEER/ is not seen then the hash is gone
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_mode
import statistics
import time
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from apps.feature_flags import load_flags
from apps.feature_flags.profiles import BUILD_PROFILE_ENV, STORE_PROFILE
from apps.shared import runtime_policy
from apps.sync.usb import stick_library as sl
from apps.sync.usb.pioneer.anlz_track import read_stick_track_analysis
from apps.sync.usb.pioneer.reader import read_export_pdb
from apps.webui.server import rb_vendor
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes import usb_tracks as usb_tracks_mod
from apps.webui.server.routes import usb_volumes as usb_mod
from apps.webui.server.routes.rb_hot_cues import HotCueSlotOut
from tests.sync.usb.synthetic_stick import (
    ARTWORK_M_BYTES,
    ARTWORK_S_BYTES,
    AUDIO_BYTES,
    BAD_TAG_GRID,
    BAD_TAG_PCOB_MS,
    FIRST_GRID,
    FIRST_HOT_CUES,
    FIRST_MEMORY_MS,
    HISTORY_ORDER,
    PLAYLIST_ENTRIES,
    PLAYLIST_ORDER,
    SHARED_ANLZ_DIR,
    SHARED_GRID,
    SHARED_HOT_CUE,
    STICK_NAME,
    STICK_UUID,
    first_track_ext,
    write_synthetic_stick,
)

API = "/api/v1/usb"
VOLUME_ID = f"vol:{STICK_UUID}"
NO_UUID_NAME = "NO UUID STICK"
# Every per-track route; the coverage tests below iterate this, so a new
# route is held to the gate, not-mounted, refusal and read-only rules here.
_TRACK_SUFFIXES = ("", "/audio", "/anlz", "/hot-cues", "/artwork")
LIBRARY_TRACK_KEYS = {
    "id", "pdb_id", "title", "artist", "album", "genre", "key", "bpm", "duration_s",
    "rating", "file_path", "has_analysis", "has_artwork", "date_added",
}


def _track_url(pdb_id: int, suffix: str = "") -> str:
    return f"{API}/tracks/usb-{STICK_UUID}-{pdb_id}{suffix}"


def _ids(pdb_ids: tuple[int, ...]) -> list[str]:
    return [f"usb-{STICK_UUID}-{pdb_id}" for pdb_id in pdb_ids]


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
        "tracks": 13, "playlists": 5, "playlist_entries": len(PLAYLIST_ENTRIES),
        "history_playlists": len(HISTORY_ORDER),
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
    assert body["playlists"][3]["track_ids"] == _ids(PLAYLIST_ORDER[3]) == _ids((1, 10, 2))
    assert body["history"] == [
        {"id": "hist-1", "name": "HISTORY 001", "track_ids": _ids(HISTORY_ORDER[1])},
        {"id": "hist-4", "name": "HISTORY 002", "track_ids": _ids(HISTORY_ORDER[4])},
    ]
    # Named by the export is not on the stick: 6/7 are outside policy, 10's files are absent.
    assert {t["pdb_id"] for t in body["tracks"] if t["has_artwork"]} == {1}
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
    for url in (
        f"{API}/volumes/{VOLUME_ID}/library",
        *(_track_url(1, suffix) for suffix in _TRACK_SUFFIXES),
    ):
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
            # Bad ids too: the gate answers before any id is parsed.
            f"{API}/volumes/path:{NO_UUID_NAME}/library",
            f"{API}/volumes/vol:{STICK_UUID.lower()}/library",
            f"{API}/volumes/garbage/library",
            f"{API}/tracks/garbage",
            *(_track_url(1, suffix) for suffix in _TRACK_SUFFIXES),
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
    for suffix in _TRACK_SUFFIXES:
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


# ----- analysis (anlz) --------------------------------------------------------

_SLOTS = "ABCDEFGH"
_HOT_CUE_SLOTS = TypeAdapter(list[HotCueSlotOut])
# The keys the deck's AnlzData requires (anlz-types.ts), taken from the
# library's own empty payload plus the two keys the library route stamps.
_ANLZ_KEYS = set(rb_vendor.empty_anlz_payload("x", 100)) | {
    "beatgrid_source",
    "beatgrid_own_unavailable_reason",
}


def _beats(grid: list[tuple[int, float, int]]) -> list[dict[str, float]]:
    return [{"n": n, "bpm": bpm, "t": round(ms / 1000, 3)} for n, bpm, ms in grid]


def _hot_cue_view(cue: tuple[int, int, int | None, int | None, str | None]) -> dict[str, Any]:
    slot, in_ms, out_ms, color, comment = cue
    return {
        "kind": "hot_cue", "slot": _SLOTS[slot], "in_ms": in_ms, "out_ms": out_ms,
        "is_loop": out_ms is not None, "active_loop": False, "beat_loop_size": None,
        "color_table_index": color, "comment": comment,
    }


def test_anlz_is_the_library_shape_from_the_tracks_own_files(client: TestClient) -> None:
    response = client.get(_track_url(1, "/anlz"), params={"points": 200, "gen": 3})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == _ANLZ_KEYS | {"unreadable_anlz"}
    assert (body["stable_id"], body["points"]) == (f"usb-{STICK_UUID}-1", 200)
    assert (body["beatgrid_source"], body["beatgrid_own_unavailable_reason"]) == ("rekordbox", None)
    assert body["beatgrid"] == {
        "source": "rekordbox", "beat_count": len(FIRST_GRID), "beats": _beats(FIRST_GRID)
    }
    assert body["vocals"] == {"status": "not_analyzed"}
    assert body["waveform"]["kind"] == "mono" and 0 < body["waveform"]["detail"]["length"] <= 200
    hot = [_hot_cue_view(cue) for cue in FIRST_HOT_CUES]
    memory = {
        **_hot_cue_view((0, FIRST_MEMORY_MS, None, None, None)), "kind": "memory", "slot": None
    }
    assert body["cues"] == sorted([*hot, memory], key=lambda cue: cue["in_ms"])
    assert [p["kind"] for p in body["phrases"]] == [1, 2] and body["unreadable_anlz"] == []


def test_anlz_tracks_sharing_one_directory_each_get_their_own(client: TestClient) -> None:
    first = client.get(_track_url(1, "/anlz")).json()
    shared = client.get(_track_url(10, "/anlz")).json()
    assert first["beatgrid"]["beats"] == _beats(FIRST_GRID)
    assert shared["beatgrid"]["beats"] == _beats(SHARED_GRID), (
        f"if ANLZ0001 in {SHARED_ANLZ_DIR} gets ANLZ0000's grid then resolution scans the dir"
    )
    assert shared["cues"] == [_hot_cue_view(SHARED_HOT_CUE)]
    assert (first["waveform"]["kind"], shared["waveform"]["kind"]) == ("mono", "tri")


def test_anlz_query_validation_matches_the_library_route(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    def query(path: str) -> list[dict[str, Any]]:
        return [p for p in paths[path]["get"]["parameters"] if p["in"] == "query"]

    assert query(f"{API}/tracks/{{track_id}}/anlz") == query("/api/v1/tracks/{stable_id}/anlz")
    for points in (runtime_policy.ANLZ_POINTS_MIN - 1, runtime_policy.ANLZ_POINTS_MAX + 1):
        assert client.get(_track_url(1, "/anlz"), params={"points": points}).status_code == 422
    default = client.get(_track_url(1, "/anlz")).json()
    assert default["points"] == runtime_policy.ANLZ_POINTS_DEFAULT


def test_anlz_etag_and_cache_headers_match_the_library_route(client: TestClient) -> None:
    first = client.get(_track_url(1, "/anlz"), params={"gen": 1})
    etag = first.headers["etag"]
    assert first.headers["cache-control"] == "private, no-cache" and etag.startswith('"')
    assert client.get(_track_url(1, "/anlz"), params={"gen": 2}).headers["etag"] == etag
    revalidated = client.get(_track_url(1, "/anlz"), headers={"If-None-Match": f"W/{etag}"})
    assert (revalidated.status_code, revalidated.content) == (304, b"")
    assert revalidated.headers["etag"] == etag
    assert client.get(_track_url(1, "/anlz"), params={"points": 100}).headers["etag"] != etag
    assert client.get(_track_url(10, "/anlz")).headers["etag"] != etag


def test_one_unreadable_tag_is_listed_and_the_rest_still_loads(client: TestClient) -> None:
    body = client.get(_track_url(13, "/anlz")).json()
    assert body["beatgrid"]["beats"] == _beats(BAD_TAG_GRID)
    assert len(body["unreadable_anlz"]) == 1
    assert body["unreadable_anlz"][0].startswith("ANLZ0000.EXT:PCO2: ValueError")
    slots = client.get(_track_url(13, "/hot-cues")).json()
    assert [(s["slot"], s["cue"]["in_ms"]) for s in slots if s["cue"]] == [("B", BAD_TAG_PCOB_MS)]


def test_unanalyzed_track_gets_the_library_empty_payload_and_empty_slots(
    client: TestClient,
) -> None:
    track_id = f"usb-{STICK_UUID}-2"
    body = client.get(_track_url(2, "/anlz"), params={"points": 300}).json()
    assert body == {
        **rb_vendor.empty_anlz_payload(track_id, 300),
        "beatgrid_source": "rekordbox",
        "beatgrid_own_unavailable_reason": None,
    }
    slots = _HOT_CUE_SLOTS.validate_python(client.get(_track_url(2, "/hot-cues")).json())
    assert [s.slot for s in slots] == list(_SLOTS) and all(s.cue is None for s in slots)


@pytest.mark.parametrize(
    "pdb_id,status,code,reason",
    [
        (8, 403, "USB_PATH_OUTSIDE_VOLUME", "outside_allowed_dir"),
        (11, 404, "ANALYSIS_NOT_FOUND", None),
        (12, 404, "USB_FILE_MISSING", None),
    ],
)
def test_anlz_and_hot_cue_refusals(
    client: TestClient, pdb_id: int, status: int, code: str, reason: str | None
) -> None:
    for suffix in ("/anlz", "/hot-cues"):
        response = client.get(_track_url(pdb_id, suffix))
        assert response.status_code == status, (suffix, response.text)
        detail = response.json()["detail"]
        assert (detail["code"], detail.get("reason")) == (code, reason), suffix
        assert detail["volume_uuid"] == STICK_UUID


# ----- hot cues ---------------------------------------------------------------


def test_hot_cues_are_the_anlz_hot_cues_in_the_library_slot_shape(
    client: TestClient, mount: Path
) -> None:
    raw = client.get(_track_url(1, "/hot-cues"))
    assert raw.status_code == 200 and "etag" not in raw.headers  # as the library GET
    slots = _HOT_CUE_SLOTS.validate_python(raw.json())
    assert [s.slot for s in slots] == list(_SLOTS)
    anlz_hot = {c["slot"]: c for c in client.get(_track_url(1, "/anlz")).json()["cues"]
                if c["kind"] == "hot_cue"}
    served = {s.slot: s.cue.model_dump(exclude={"revision"}) for s in slots if s.cue}
    assert served == anlz_hot == {_SLOTS[c[0]]: _hot_cue_view(c) for c in FIRST_HOT_CUES}
    # The decoder's own hot-cue list agrees slot for slot (position, color, label).
    decoded = read_stick_track_analysis(
        volume_root=mount, analyze_path=f"/{SHARED_ANLZ_DIR}/ANLZ0000.DAT", points=100
    ).hot_cues
    assert [(_SLOTS[c["slot"]], c["position_ms"], c["color"], c["label"]) for c in decoded] == [
        (slot, cue["in_ms"], cue["color_table_index"], cue["comment"])
        for slot, cue in sorted(served.items())
    ]
    for s in slots:
        assert len(s.revision) == 64 and (s.cue is None or s.cue.revision == s.revision)
    assert len({s.revision for s in slots}) == len(_SLOTS)
    again = _HOT_CUE_SLOTS.validate_python(client.get(_track_url(1, "/hot-cues")).json())
    assert [s.revision for s in again] == [s.revision for s in slots]
    other = _HOT_CUE_SLOTS.validate_python(client.get(_track_url(10, "/hot-cues")).json())
    assert not {s.revision for s in other} & {s.revision for s in slots}


def test_a_changed_cue_changes_only_its_own_slot_revision(client: TestClient, mount: Path) -> None:
    def revisions() -> dict[str, str]:
        slots = _HOT_CUE_SLOTS.validate_python(client.get(_track_url(1, "/hot-cues")).json())
        return {s.slot: s.revision for s in slots}

    before = revisions()
    moved_a = (0, 200, None, 43, "Drop")
    (mount / SHARED_ANLZ_DIR / "ANLZ0000.EXT").write_bytes(
        first_track_ext((moved_a, FIRST_HOT_CUES[1]))
    )
    after = revisions()
    assert after["A"] != before["A"], "if a moved cue keeps its revision then it names no state"
    assert {k: v for k, v in after.items() if k != "A"} == {
        k: v for k, v in before.items() if k != "A"
    }


def test_two_hot_cues_in_one_slot_fail_loud_rather_than_drop_one() -> None:
    cue = {**_hot_cue_view(FIRST_HOT_CUES[0])}
    with pytest.raises(RuntimeError, match="two hot cues in one slot"):
        usb_tracks_mod._hot_cue_slots("usb-x-1", [cue, {**cue, "in_ms": 999}])


def test_hot_cues_have_no_write_route(client: TestClient) -> None:
    for method in ("put", "post", "delete", "patch"):
        assert client.request(method, _track_url(1, "/hot-cues")).status_code == 405
        assert client.request(method, _track_url(1, "/hot-cues/A")).status_code == 404


# ----- TrackOut predicts the routes -------------------------------------------


def test_trackout_flags_predict_what_the_stick_routes_serve(client: TestClient) -> None:
    library = client.get(f"{API}/volumes/{VOLUME_ID}/library").json()
    checked = 0
    for row in library["tracks"]:
        track = client.get(_track_url(row["pdb_id"]))
        if track.status_code != 200:
            continue
        checked += 1
        body = track.json()
        artwork_ok = all(
            client.get(_track_url(row["pdb_id"], "/artwork"), params={"size": size}).status_code
            == 200
            for size in ("s", "m")
        )
        assert body["artwork_available"] is artwork_ok, row["pdb_id"]
        assert row["has_artwork"] is artwork_ok, (row["pdb_id"], "listing and TrackOut disagree")
        assert body["has_rb_mapping"] is False, "hot cues are read only: no SAVE may be offered"
        anlz = client.get(_track_url(row["pdb_id"], "/anlz"))
        hot = client.get(_track_url(row["pdb_id"], "/hot-cues"))
        assert anlz.status_code == hot.status_code, row["pdb_id"]
        if anlz.status_code == 200:
            assert row["has_analysis"] is bool(anlz.json()["beatgrid"]["beats"]), row["pdb_id"]
    assert checked >= 8, f"only {checked} tracks resolved; the invariant was barely exercised"


# ----- USBPLAY-08: nothing on the stick changes ---------------------------------

_WATCHED_DIRS = ("PIONEER", "Contents")
#: (size, mtime_ns, sha256 of a hashed regular file else None) per relative path.
Snapshot = dict[str, tuple[int, int, str | None]]


def _stick_snapshot(mount: Path, hash_dirs: tuple[str, ...] = ("PIONEER",)) -> Snapshot:
    """Every file AND directory under PIONEER/ and Contents/, each regular
    file under ``hash_dirs`` hashed (USBPLAY-08's hash check).

    The hash sees a rewrite that keeps size and mtime; size and mtime cover
    the rest (a real stick's Contents/ is too big to hash per run, so the
    audio files a test streams are hashed by :func:`_digests` instead).
    Directories are included so a file created and deleted again, or an
    added AppleDouble ``._`` sibling, still shows as a changed mtime.
    """
    snapshot: Snapshot = {}
    for top in _WATCHED_DIRS:
        root = mount / top
        if not root.is_dir():
            raise AssertionError(f"{root} is not a directory; the snapshot would watch nothing")
        for dirpath, dirnames, filenames in os.walk(root, onerror=_raise):
            for name in (".", *dirnames, *filenames):
                path = Path(dirpath, name)
                st = path.lstat()
                hashed = top in hash_dirs and stat_mode.S_ISREG(st.st_mode)
                snapshot[str(path.relative_to(mount))] = (
                    st.st_size, st.st_mtime_ns, _sha256(path) if hashed else None
                )
    return snapshot


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _digests(mount: Path, stick_paths: list[str]) -> dict[str, str]:
    return {p: _sha256(mount / p.lstrip("/")) for p in stick_paths}


def _raise(error: OSError) -> None:
    raise error


def _snapshot_diff(before: Snapshot, after: Snapshot) -> str:
    changed = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
    return (
        f"added {sorted(after.keys() - before.keys())[:10]}, "
        f"removed {sorted(before.keys() - after.keys())[:10]}, changed {changed[:10]}"
    )


def test_stick_snapshot_detects_a_change_and_the_routes_make_none(
    client: TestClient, mount: Path
) -> None:
    every_dir = ("PIONEER", "Contents")
    before = _stick_snapshot(mount, hash_dirs=every_dir)
    assert "PIONEER/rekordbox/export.pdb" in before, "control: the snapshot sees the export"
    assert f"{SHARED_ANLZ_DIR}/ANLZ0001.2EX" in before
    assert before["Contents/second.flac"][2] is not None, "control: audio is hashed here"
    for pdb_id in range(1, 14):
        for suffix in _TRACK_SUFFIXES:
            client.get(_track_url(pdb_id, suffix))
        client.head(_track_url(pdb_id, "/audio"))
    client.get(f"{API}/volumes/{VOLUME_ID}/library")
    after = _stick_snapshot(mount, hash_dirs=every_dir)
    assert after == before, _snapshot_diff(before, after)
    # The guard bites: a touched mtime, and an added file, each read as a change.
    dat_key = f"{SHARED_ANLZ_DIR}/ANLZ0000.DAT"
    dat = mount / dat_key
    os.utime(dat, ns=(dat.stat().st_atime_ns, dat.stat().st_mtime_ns + 1))
    assert _stick_snapshot(mount) != before
    os.utime(dat, ns=(dat.stat().st_atime_ns, before[dat_key][1]))
    assert _stick_snapshot(mount, hash_dirs=every_dir) == before, "control: mtime restored"
    # A rewrite that keeps size AND mtime is seen by the hash alone.
    original = dat.read_bytes()
    dat.write_bytes(bytes(b ^ 0xFF for b in original))
    os.utime(dat, ns=(dat.stat().st_atime_ns, before[dat_key][1]))
    rewritten = _stick_snapshot(mount, hash_dirs=every_dir)
    assert rewritten[dat_key][:2] == before[dat_key][:2], "control: size and mtime unchanged"
    assert rewritten[dat_key][2] != before[dat_key][2], "if unseen then the hash check is gone"
    (mount / "Contents" / "._second.flac").write_bytes(b"x")
    assert _stick_snapshot(mount).keys() - before.keys() == {"Contents/._second.flac"}


# ----- read only and documented ---------------------------------------------


def test_no_write_method_is_routed_under_usb_tracks(client: TestClient) -> None:
    stick_routes = [
        route for route in client.app.routes  # type: ignore[attr-defined]
        if isinstance(route, APIRoute) and route.path.startswith(f"{API}/tracks")
    ]
    # Positive control: the scan sees this lane's routes (lane A2 adds more,
    # and they are held to the same rule without editing this test).
    assert {f"{API}/tracks/{{track_id}}{s}" for s in _TRACK_SUFFIXES} <= {
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
        *(f"{API}/tracks/{{track_id}}{suffix}" for suffix in _TRACK_SUFFIXES),
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
    raw = read_export_pdb(live_mount)
    _assert_live_playlist_order(body, raw)
    _assert_live_artwork_flags(body, raw, live_mount)


def _assert_live_playlist_order(body: dict[str, Any], raw: dict[str, Any]) -> None:
    """USBPLAY-05: each playlist is the raw rows sorted by entry_index."""
    uuid = body["volume_uuid"]
    rows: dict[int, list[tuple[int, int]]] = {}
    for entry in raw["playlist_entries"]:
        rows.setdefault(entry["playlist_id"], []).append((entry["entry_index"], entry["track_id"]))
    served = {p["pdb_id"]: p["track_ids"] for p in body["playlists"]}
    expected = {
        pl["id"]: [f"usb-{uuid}-{t}" for _, t in sorted(rows.get(pl["id"], []))]
        for pl in raw["playlists"]
    }
    assert served == expected
    by_track_id = sum(
        1 for pl_rows in rows.values()
        if [t for _, t in sorted(pl_rows)] != sorted(t for _, t in pl_rows)
    )
    assert by_track_id > 0, "control: no playlist here tells entry order from track id order"
    history_ids = [int(h["id"].removeprefix("hist-")) for h in body["history"]]
    assert history_ids == sorted(history_ids), "history playlists come in id order"
    print(f"live order: {by_track_id} of {len(rows)} playlists differ from track id order")


def _assert_live_artwork_flags(
    body: dict[str, Any], raw: dict[str, Any], live_mount: Path
) -> None:
    """has_artwork is both served files on the stick, checked here without the resolver."""
    named = {
        t["id"]: raw["artwork"].get(t["artwork_id"]) for t in raw["tracks"] if t["artwork_id"]
    }
    on_stick: set[int] = set()
    for pdb_id, small in named.items():
        if small is None:
            continue  # an artwork id with no artwork row names no file
        small_path = live_mount / small.lstrip("/")
        if small_path.is_file() and small_path.with_name(
            f"{small_path.stem}_m{small_path.suffix}"
        ).is_file():
            on_stick.add(pdb_id)
    served = {t["pdb_id"] for t in body["tracks"] if t["has_artwork"]}
    print(f"live artwork: {len(named)} named, {len(on_stick)} on the stick, {len(served)} served")
    assert served == on_stick


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


# The reference stick's track 36 (the track Play from USB was first played
# with), the stick track with the most hot cues (so a real colored cue is
# proven to cross the route; 5 of the reference stick's 563 carry any), and
# four more picked evenly from the analyzed, present tracks.
_LIVE_ANCHOR_PDB_ID = 36
_LIVE_EVEN_TRACKS = 4


def _timed(call: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    result = call()
    return result, (time.perf_counter() - started) * 1000.0


def _playable_analyzed(tracks: list[dict[str, Any]], live_mount: Path) -> list[dict[str, Any]]:
    return [
        t for t in tracks
        if t["has_analysis"] and live_mount.joinpath(t["file_path"].lstrip("/")).is_file()
    ]


def _hot_cue_sweep(
    client: TestClient, candidates: list[dict[str, Any]]
) -> dict[int, list[HotCueSlotOut]]:
    """GET /hot-cues for every candidate; each must answer. Returns the ones with a cue."""
    with_cues: dict[int, list[HotCueSlotOut]] = {}
    for row in candidates:
        response = client.get(f"{API}/tracks/{row['id']}/hot-cues")
        assert response.status_code == 200, (row["id"], response.text[:300])
        slots = _HOT_CUE_SLOTS.validate_python(response.json())
        if any(s.cue for s in slots):
            with_cues[row["pdb_id"]] = slots
    return with_cues


def _live_picks(
    tracks: list[dict[str, Any]], candidates: list[dict[str, Any]], hot_cue_pdb_id: int
) -> list[dict[str, Any]]:
    by_pdb_id = {t["pdb_id"]: t for t in tracks}
    if _LIVE_ANCHOR_PDB_ID not in by_pdb_id:
        pytest.fail(f"the stick's export has no track {_LIVE_ANCHOR_PDB_ID}; wrong stick?")
    fixed = {_LIVE_ANCHOR_PDB_ID, hot_cue_pdb_id}
    rest = [t for t in candidates if t["pdb_id"] not in fixed]
    step = len(rest) // _LIVE_EVEN_TRACKS
    assert step > 0, f"only {len(rest)} more analyzed, present tracks on the stick"
    even = [rest[i * step] for i in range(_LIVE_EVEN_TRACKS)]
    return [by_pdb_id[_LIVE_ANCHOR_PDB_ID], by_pdb_id[hot_cue_pdb_id], *even]


def test_live_stick_tracks_play_and_the_stick_is_never_written(
    live_client: TestClient, live_mount: Path
) -> None:
    """USBPLAY-06/07/08 on the real stick: TrackOut, audio HEAD + Range, the
    stick's own grid and cues, and no file under PIONEER/ or Contents/ changes."""
    before, snapshot_ms = _timed(lambda: _stick_snapshot(live_mount))
    assert "PIONEER/rekordbox/export.pdb" in before, "control: the snapshot sees the export"
    assert any(k.startswith("Contents/") for k in before), "control: the snapshot sees audio"
    volume_id = _live_volume_id(live_client, live_mount)
    library = live_client.get(f"{API}/volumes/{volume_id}/library")
    assert library.status_code == 200, library.text
    all_tracks = library.json()["tracks"]
    candidates = _playable_analyzed(all_tracks, live_mount)
    with_cues, sweep_ms = _timed(lambda: _hot_cue_sweep(live_client, candidates))
    assert with_cues, "control: no stick track served a hot cue, so none was proven to cross"
    richest = max(with_cues, key=lambda pdb_id: sum(1 for s in with_cues[pdb_id] if s.cue))
    richest_cues = [s.cue for s in with_cues[richest] if s.cue]
    assert all(cue.color_table_index is not None for cue in richest_cues), (
        f"a hot cue lost its color: {richest_cues}"
    )
    print(
        f"live hot-cue sweep: {len(candidates)} tracks in {sweep_ms:.0f} ms, "
        f"{len(with_cues)} with hot cues, richest pdb {richest} "
        f"slots {[(c.slot, c.color_table_index) for c in richest_cues]}"
    )
    tracks = _live_picks(all_tracks, candidates, richest)
    # First opened in the loop below (TrackOut does not stat audio): hash them now.
    audio_before = _digests(live_mount, [row["file_path"] for row in tracks])
    timings: dict[str, list[float]] = {}
    for row in tracks:
        base = f"{API}/tracks/{row['id']}"
        track, ms = _timed(partial(live_client.get, base))
        timings.setdefault("track", []).append(ms)
        assert track.status_code == 200 and track.json()["stable_id"] == row["id"], track.text
        head, ms = _timed(partial(live_client.head, f"{base}/audio"))
        timings.setdefault("audio_head", []).append(ms)
        assert head.status_code == 200 and int(head.headers["content-length"]) > 1024, row["id"]
        ranged, ms = _timed(
            partial(live_client.get, f"{base}/audio", headers={"Range": "bytes=0-1023"})
        )
        timings.setdefault("audio_range", []).append(ms)
        assert ranged.status_code == 206 and len(ranged.content) == 1024, row["id"]
        assert ranged.headers["content-range"].startswith("bytes 0-1023/"), row["id"]
        anlz, ms = _timed(partial(live_client.get, f"{base}/anlz"))
        timings.setdefault("anlz", []).append(ms)
        assert anlz.status_code == 200, (row["id"], anlz.text[:300])
        body = anlz.json()
        assert set(body) >= _ANLZ_KEYS, sorted(_ANLZ_KEYS - set(body))
        assert body["beatgrid"]["source"] == "rekordbox" and body["beatgrid_source"] == "rekordbox"
        assert body["beatgrid"]["beats"], f"{row['id']} served an empty grid"
        assert body["vocals"]["status"] == "not_analyzed", body["vocals"]
        assert body["waveform"]["detail"]["length"] > 0, row["id"]
        hot, ms = _timed(partial(live_client.get, f"{base}/hot-cues"))
        timings.setdefault("hot_cues", []).append(ms)
        assert hot.status_code == 200, (row["id"], hot.text[:300])
        slots = _HOT_CUE_SLOTS.validate_python(hot.json())
        served = {s.slot: s.cue.model_dump(exclude={"revision"}) for s in slots if s.cue}
        assert served == {c["slot"]: c for c in body["cues"] if c["kind"] == "hot_cue"}
        print(
            f"live track pdb {row['pdb_id']}: {len(body['beatgrid']['beats'])} beats, "
            f"{len(served)} hot cues, {len(body['cues'])} cues, "
            f"waveform {body['waveform']['kind']}, unreadable {len(body['unreadable_anlz'])}"
        )
    with_art = next(t for t in all_tracks if t["has_artwork"])
    assert live_client.get(f"{API}/tracks/{with_art['id']}/artwork").status_code == 200
    after = _stick_snapshot(live_mount)
    audio_after = _digests(live_mount, list(audio_before))
    print(
        "live timings ms (median / max over "
        f"{len(tracks)} tracks): "
        + ", ".join(
            f"{name} {statistics.median(values):.1f}/{max(values):.1f}"
            for name, values in timings.items()
        )
        + f"; snapshot of {len(before)} entries, PIONEER/ hashed, {snapshot_ms:.0f} ms"
    )
    assert after == before, f"the stick changed during play: {_snapshot_diff(before, after)}"
    assert audio_after == audio_before, "a streamed audio file's bytes changed"
