"""Play from USB resolver: ids, cache, containment (USBPLAY-03/04/06).

Every stick here is a synthetic ``export.pdb`` from ``export_pdb_builder``
under ``tmp_path``, read by the production Kaitai reader: no mocks, no real
stick, nothing copied from one.

Regression intent, one line per guard:
  - if a minted stick id does not parse back to the same (uuid, pdb id) then ids are broken
  - if parse accepts a colon, lowercase uuid, leading zero or missing prefix then parse is loose
  - if Open Key 6m/9m/1d do not become Abm/Fm/C then deck key sync breaks on sticks
  - if a second open re-parses or re-scans (or takes 100 ms) then the cache is broken
  - if an mtime or size change serves the old parse then the cache is stale
  - if a vanished stick answers anything but USB_STICK_NOT_MOUNTED then the load toast lies
  - if ``..`` or a symlink out of the mount is served then containment is broken
  - if a non-audio file is served as audio then the extension allowlist is broken
  - if artwork outside PIONEER/Artwork or not .jpg is served then artwork policy is broken
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from apps.shared.harmonic import CamelotKey, key_to_camelot
from apps.shared.stable_id import is_safe_stable_id_segment
from apps.sync.usb import stick_library as sl
from tests.sync.usb.export_pdb_builder import PdbTrack, write_export_pdb
from tests.sync.usb.synthetic_stick import (
    OTHER_UUID,
    STICK_NAME,
    STICK_UUID,
    synthetic_export,
    synthetic_tracks,
    write_synthetic_stick,
)

UUID = STICK_UUID


@pytest.fixture(autouse=True)
def _fresh_resolver_state() -> None:
    sl._reset_for_tests()


@pytest.fixture
def mount(tmp_path: Path) -> Path:
    return write_synthetic_stick(tmp_path / "Volumes")


class CountingScan:
    """A real callable standing in for discovery: reports what is on disk."""

    def __init__(self, volumes: list[sl.MountedVolume]) -> None:
        self.volumes = volumes
        self.calls = 0

    def __call__(self) -> Sequence[sl.MountedVolume]:
        self.calls += 1
        return list(self.volumes)


def _volume(mount: Path, volume_uuid: str | None = UUID) -> sl.MountedVolume:
    return sl.MountedVolume(
        volume_id=f"vol:{volume_uuid}", volume_uuid=volume_uuid, name=STICK_NAME, mount_path=mount
    )


@pytest.fixture
def scan(mount: Path) -> CountingScan:
    return CountingScan([_volume(mount)])


def _refusal(action: Callable[[], object]) -> sl.StickError:
    with pytest.raises(sl.StickError) as caught:
        action()
    return caught.value


def _resolve(pdb_id: int, scan: CountingScan) -> sl.ResolvedStickTrack:
    return sl.resolve_stick_track(sl.mint_stick_track_id(UUID, pdb_id), scan)


# ----- ids ------------------------------------------------------------------


def test_minted_id_round_trips_and_is_a_safe_path_segment() -> None:
    track_id = sl.mint_stick_track_id(UUID, 36)
    assert track_id == f"usb-{UUID}-36"
    assert is_safe_stable_id_segment(track_id)
    assert sl.parse_stick_track_id(track_id) == sl.StickTrackRef(volume_uuid=UUID, pdb_id=36)
    assert sl.parse_stick_track_id(sl.mint_stick_track_id(UUID, 2**32 - 1)).pdb_id == 2**32 - 1


@pytest.mark.parametrize(
    "bad_id",
    [
        f"usb:{UUID}-36",
        f"usb-{UUID.lower()}-36",
        f"usb-{UUID}-036",
        f"usb-{UUID}-0",
        f"usb-{UUID}-4294967296",
        f"{UUID}-36",
        f"USB-{UUID}-36",
        f"usb-{UUID}-",
        f"usb-{UUID}-36x",
        f"usb-{UUID}-3/6",
        f"usb-{UUID}",
        "usb--36",
        "",
    ],
)
def test_parse_refuses_anything_mint_would_not_produce(bad_id: str) -> None:
    assert _refusal(lambda: sl.parse_stick_track_id(bad_id)).code == "USB_TRACK_ID_INVALID"


@pytest.mark.parametrize("volume_uuid,pdb_id", [(UUID.lower(), 1), (UUID, 0), (UUID, True)])
def test_mint_refuses_ids_parse_would_not_round_trip(volume_uuid: str, pdb_id: int) -> None:
    with pytest.raises(ValueError):
        sl.mint_stick_track_id(volume_uuid, pdb_id)


# ----- keys -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("6m", "Abm"), ("9m", "Fm"), ("1m", "Am"), ("1d", "C"), ("12d", "F"), (" Am ", "Am"),
     ("8A", "8A"), ("F#m", "F#m"), ("", None), (None, None)],
)
def test_open_key_becomes_musical_and_others_pass_through(raw: str | None, expected: str) -> None:
    assert sl.normalize_key(raw) == expected


@pytest.mark.parametrize("mode", ["m", "d"])
@pytest.mark.parametrize("number", range(1, 13))
def test_every_open_key_lands_on_its_camelot_position(number: int, mode: str) -> None:
    musical = sl.normalize_key(f"{number}{mode}")
    assert musical is not None
    expected = CamelotKey(number=(number + 6) % 12 + 1, letter="A" if mode == "m" else "B")
    assert key_to_camelot(musical) == expected


# ----- library model --------------------------------------------------------


def test_library_model_trims_display_strings_and_keeps_paths(
    mount: Path, scan: CountingScan
) -> None:
    library = sl.open_stick_library(UUID, scan).library
    first = library.tracks_by_pdb_id[1]
    assert first.id == f"usb-{UUID}-1"
    assert (first.title, first.artist, first.album, first.genre) == (
        "First Synthetic", "Synth Artist", "Synth Album", "Techno"
    )
    assert (first.key, first.bpm, first.duration_s, first.rating) == ("Abm", 124.5, 301.0, 4)
    assert first.file_path == "/Contents/Synth Artist/First Synthetic .mp3"
    assert first.has_analysis and first.has_artwork
    assert first.artwork_path == "/PIONEER/Artwork/00001/a1.jpg"
    assert first.date_added == "2026-09-01"
    second = library.tracks_by_pdb_id[2]
    assert (second.artist, second.key, second.duration_s) == (None, "Am", None)
    assert not second.has_analysis and not second.has_artwork
    assert len(library.tracks) == 9 and library.playlist_entry_count == 3


def test_playlists_come_in_tree_order_with_entries_in_entry_order(scan: CountingScan) -> None:
    library = sl.open_stick_library(UUID, scan).library
    assert [p.id for p in library.playlists] == ["pl-5", "pl-7", "pl-6", "pl-3", "pl-9"]
    by_id = {p.id: p for p in library.playlists}
    assert by_id["pl-5"].is_folder and by_id["pl-5"].name == "Folder"
    assert by_id["pl-7"].parent_id == "pl-5" and by_id["pl-3"].parent_id is None
    assert by_id["pl-3"].track_ids == (f"usb-{UUID}-1", f"usb-{UUID}-2")
    assert [(h.id, h.name, h.track_ids) for h in library.history] == [
        ("hist-1", "HISTORY 001", (f"usb-{UUID}-1", f"usb-{UUID}-2"))
    ]


def test_a_stick_named_pioneer_still_reads_its_own_pioneer_dir(tmp_path: Path) -> None:
    root = tmp_path / "PIONEER"
    write_export_pdb(root, synthetic_export())
    library = sl.open_stick_library(UUID, CountingScan([_volume(root)])).library
    assert len(library.tracks) == 9


# ----- binding and cache ----------------------------------------------------


def test_second_open_is_a_cache_hit_without_rescan(scan: CountingScan) -> None:
    cold = sl.open_stick_library(UUID, scan)
    started = time.perf_counter()
    warm = sl.open_stick_library(UUID, scan)
    warm_ms = (time.perf_counter() - started) * 1000.0
    print(f"warm open_stick_library: {warm_ms:.2f} ms")
    assert not cold.cache_hit and warm.cache_hit
    assert warm.library is cold.library
    assert scan.calls == 1
    assert warm_ms < 100.0


def test_mtime_change_reparses_after_a_fresh_scan(mount: Path, scan: CountingScan) -> None:
    cold = sl.open_stick_library(UUID, scan)
    pdb = mount.joinpath(*sl.EXPORT_PDB_PARTS)
    stat = pdb.stat()
    os.utime(pdb, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    again = sl.open_stick_library(UUID, scan)
    assert not again.cache_hit and again.library is not cold.library
    assert scan.calls == 2


def test_size_change_at_the_same_mtime_reparses(mount: Path, scan: CountingScan) -> None:
    sl.open_stick_library(UUID, scan)
    pdb = mount.joinpath(*sl.EXPORT_PDB_PARTS)
    before = pdb.stat()
    # Pages are fixed-size, so the export only grows once a new page is needed.
    extra = [
        PdbTrack(id=10 + n, title="Added later " + "x" * 100, file_path="/Contents/second.flac")
        for n in range(40)
    ]
    write_export_pdb(mount, synthetic_export([*synthetic_tracks(), *extra]))
    os.utime(pdb, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert pdb.stat().st_size != before.st_size
    again = sl.open_stick_library(UUID, scan)
    assert not again.cache_hit and 10 in again.library.tracks_by_pdb_id


def test_unmounted_stick_is_not_mounted_with_its_uuid(mount: Path) -> None:
    error = _refusal(lambda: sl.open_stick_library(UUID, CountingScan([])))
    assert error.code == "USB_STICK_NOT_MOUNTED"
    assert error.to_detail()["volume_uuid"] == UUID


def test_swapped_stick_at_the_same_path_is_not_served(mount: Path, scan: CountingScan) -> None:
    sl.open_stick_library(UUID, scan)
    write_export_pdb(mount, synthetic_export(synthetic_tracks()[:2]))
    scan.volumes = [_volume(mount, OTHER_UUID)]
    error = _refusal(lambda: sl.open_stick_library(UUID, scan))
    assert error.code == "USB_STICK_NOT_MOUNTED"


def test_removed_mount_rebinds_and_reports_not_mounted(mount: Path, scan: CountingScan) -> None:
    sl.open_stick_library(UUID, scan)
    mount.rename(mount.with_name("gone"))
    scan.volumes = []
    assert _refusal(lambda: sl.open_stick_library(UUID, scan)).code == "USB_STICK_NOT_MOUNTED"


def test_stick_without_export_is_file_missing(tmp_path: Path) -> None:
    bare = tmp_path / "BARE"
    bare.mkdir()
    error = _refusal(lambda: sl.open_stick_library(UUID, CountingScan([_volume(bare)])))
    assert error.code == "USB_FILE_MISSING"


def test_unreadable_export_is_access_blocked(mount: Path, scan: CountingScan) -> None:
    pdb = mount.joinpath(*sl.EXPORT_PDB_PARTS)
    pdb.chmod(0)
    try:
        if os.access(pdb, os.R_OK):
            pytest.skip("running as a user chmod 000 does not block (root)")
        error = _refusal(lambda: sl.open_stick_library(UUID, scan))
    finally:
        pdb.chmod(0o644)
    assert error.code == "USB_STICK_ACCESS_BLOCKED"


def test_duplicate_uuid_in_one_scan_is_an_explicit_error(mount: Path) -> None:
    with pytest.raises(RuntimeError, match="share VolumeUUID"):
        sl.open_stick_library(UUID, CountingScan([_volume(mount), _volume(mount)]))


def test_unknown_pdb_id_is_track_not_found(scan: CountingScan) -> None:
    assert _refusal(lambda: _resolve(404, scan)).code == "USB_TRACK_NOT_FOUND"


# ----- files ----------------------------------------------------------------


def test_audio_is_served_from_under_the_mount(mount: Path, scan: CountingScan) -> None:
    audio = sl.stick_audio_file(_resolve(1, scan))
    assert audio.path == mount.resolve() / "Contents" / "Synth Artist" / "First Synthetic .mp3"
    assert audio.media_type == "audio/mpeg"
    assert sl.stick_audio_file(_resolve(2, scan)).media_type == "audio/flac"


@pytest.mark.parametrize(
    "pdb_id,reason",
    [(4, "escapes_mount"), (5, "escapes_mount"), (3, "extension_not_allowed"), (9, "invalid_path")],
)
def test_audio_outside_policy_is_refused(scan: CountingScan, pdb_id: int, reason: str) -> None:
    error = _refusal(lambda: sl.stick_audio_file(_resolve(pdb_id, scan)))
    assert error.code == "USB_PATH_OUTSIDE_VOLUME"
    assert error.to_detail()["reason"] == reason


def test_host_absolute_pdb_path_is_joined_under_the_mount(
    tmp_path: Path, mount: Path, scan: CountingScan
) -> None:
    host_file = tmp_path / "host-secret.mp3"
    resolved = _resolve(1, scan)
    host_track = replace(resolved.track, file_path=str(host_file))
    target = replace(resolved, track=host_track)
    assert sl.stick_audio_path(target).is_relative_to(mount.resolve())
    assert _refusal(lambda: sl.stick_audio_file(target)).code == "USB_FILE_MISSING"


def test_audio_path_needs_no_stat_but_audio_file_does(mount: Path, scan: CountingScan) -> None:
    resolved = _resolve(2, scan)
    (mount / "Contents" / "second.flac").unlink()
    assert sl.stick_audio_path(resolved).name == "second.flac"
    assert _refusal(lambda: sl.stick_audio_file(resolved)).code == "USB_FILE_MISSING"


def test_artwork_sizes_pick_the_pdb_jpg_or_its_m_sibling(mount: Path, scan: CountingScan) -> None:
    resolved = _resolve(1, scan)
    artwork_dir = mount.resolve() / "PIONEER" / "Artwork" / "00001"
    assert sl.stick_artwork_file(resolved, "s") == artwork_dir / "a1.jpg"
    assert sl.stick_artwork_file(resolved, "m") == artwork_dir / "a1_m.jpg"
    assert sl.stick_artwork_file(resolved, "orig") == artwork_dir / "a1_m.jpg"


@pytest.mark.parametrize(
    "pdb_id,code,reason",
    [
        (6, "USB_PATH_OUTSIDE_VOLUME", "outside_allowed_dir"),
        (7, "USB_PATH_OUTSIDE_VOLUME", "extension_not_allowed"),
        (2, "USB_FILE_MISSING", None),
    ],
)
def test_artwork_outside_policy_is_refused(
    scan: CountingScan, pdb_id: int, code: str, reason: str | None
) -> None:
    error = _refusal(lambda: sl.stick_artwork_file(_resolve(pdb_id, scan), "s"))
    assert error.code == code
    assert error.to_detail().get("reason") == reason


def test_anlz_comes_from_the_exact_analyze_path(mount: Path, scan: CountingScan) -> None:
    resolved = _resolve(1, scan)
    anlz_dir = mount.resolve() / "PIONEER" / "USBANLZ" / "P001" / "0000A001"
    assert sl.stick_anlz_file(resolved, ".DAT") == anlz_dir / "ANLZ0000.DAT"
    assert sl.stick_anlz_file(resolved, ".EXT") == anlz_dir / "ANLZ0000.EXT"
    assert _refusal(lambda: sl.stick_anlz_file(resolved, ".2EX")).code == "USB_FILE_MISSING"
    outside = _refusal(lambda: sl.stick_anlz_file(_resolve(8, scan), ".DAT"))
    assert outside.to_detail()["reason"] == "outside_allowed_dir"
    assert _refusal(lambda: sl.stick_anlz_file(_resolve(2, scan), ".DAT")).code == (
        "USB_FILE_MISSING"
    )
