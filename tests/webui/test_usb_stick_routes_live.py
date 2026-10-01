"""Live read-only tests against a real rekordbox USB stick when MDT_USB_STICK_ROOT is set.

Fails closed when ``MDT_USB_STICK_ROOT`` is unset (``MDT_ALLOW_MISSING_FIXTURES=1`` reports
the stick UNAVAILABLE as a skip instead, see tests/sync/usb/live_stick.py); never writes to
the stick.
USBPLAY-08 snapshot helpers and the synthetic stick write guard live here with the real-stick
suite so the route tests module stays under the 600-line limit.
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
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.sync.usb import stick_library as sl
from apps.sync.usb.pioneer.reader import read_export_pdb
from apps.webui.server.routes import usb_volumes as usb_mod
from apps.webui.server.routes.rb_hot_cues import HotCueSlotOut
from tests.sync.usb.live_stick import live_stick_root
from tests.sync.usb.synthetic_stick import SHARED_ANLZ_DIR

from .test_usb_stick_routes import (
    _ANLZ_KEYS,
    _HOT_CUE_SLOTS,
    _TRACK_SUFFIXES,
    API,
    VOLUME_ID,
    _client,
    _track_url,
)

pytest_plugins = ["tests.webui.test_usb_stick_routes"]

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


# ----- live: a real stick, read only -----------------------------------------

# The reference test stick's export (read Fri 25 Sep 2026). Another stick
# needs its own numbers; these are what USBPLAY-03 was measured against.
_LIVE_COUNTS = {"tracks": 563, "playlists": 10, "playlist_entries": 759}
_LIVE_COLD_BUDGET_MS = 2000.0


@pytest.fixture(autouse=True)
def _fresh_state() -> Iterator[None]:
    usb_mod._reset_state_for_tests()
    sl._reset_for_tests()
    yield
    usb_mod._reset_state_for_tests()
    sl._reset_for_tests()


@pytest.fixture
def live_mount() -> Path:
    return live_stick_root(os.environ)


@pytest.fixture
def live_client(
    live_mount: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
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
