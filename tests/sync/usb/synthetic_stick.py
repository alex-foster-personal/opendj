"""A synthetic Play from USB stick for tests: one ``export.pdb`` from
``export_pdb_builder`` plus the files its rows point at, including rows that
must be REFUSED (a ``..`` escape, a symlink out of the mount, a non-audio
file, artwork outside PIONEER/Artwork, a NUL byte). All names are invented;
nothing is copied from a real stick.

ANLZ files are real PMAI containers built by ``anlz_bytes``: track 1 and
track 10 share one ANLZ directory (``ANLZ0000.*`` and ``ANLZ0001.*``), track
11's .DAT is not a PMAI container, track 12 names a .DAT that is absent, and
track 13's .EXT carries a PCO2 tag that will not decode.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from tests.sync.usb.anlz_bytes import (
    grid,
    pco2,
    pcob,
    pcp2,
    pcpt,
    pmai,
    pqtz,
    pssi,
    pwav,
    pwv5,
    pwv6,
    pwv7,
)
from tests.sync.usb.export_pdb_builder import PdbExport, PdbPlaylist, PdbTrack, write_export_pdb

STICK_UUID = "0A1B2C3D-4E5F-4061-8293-A4B5C6D7E8F9"
OTHER_UUID = "11111111-2222-4333-8444-555555555555"
STICK_NAME = "SYN STICK "  # trailing space on purpose, like the real test stick
AUDIO_BYTES = b"\xff\xfb\x90\x00synthetic audio payload"
ARTWORK_S_BYTES = b"small"
ARTWORK_M_BYTES = b"medium"

SHARED_ANLZ_DIR = "PIONEER/USBANLZ/P001/0000A001"
# Track 1 (ANLZ0000): PCO2 hot cues A (commented, colored) and C (a loop), one
# memory cue, a mono waveform (no .2EX) and two phrases.
FIRST_GRID = grid(152, 124.5, 16)
FIRST_HOT_CUES = (  # (slot index 0..7, in_ms, out_ms, color, comment)
    (0, 152, None, 43, "Drop"),
    (2, 7890, 9800, 19, None),
)
FIRST_MEMORY_MS = 4000
# Track 10 (ANLZ0001, same directory): its own grid, one hot cue E, tri waveform.
SHARED_GRID = grid(19, 132.0, 12)
SHARED_HOT_CUE = (4, 19, None, 19, None)
# Track 13: grid fine, PCO2 hot list corrupt -> hot cue B from the .DAT PCOB copy.
BAD_TAG_GRID = grid(100, 120.0, 8)
BAD_TAG_PCOB_MS = 1500


def synthetic_tracks() -> list[PdbTrack]:
    return [
        PdbTrack(
            id=1,
            title="  First Synthetic  ",
            file_path="/Contents/Synth Artist/First Synthetic .mp3",
            analyze_path="/PIONEER/USBANLZ/P001/0000A001/ANLZ0000.DAT",
            artist_id=1,
            album_id=1,
            genre_id=1,
            key_id=1,
            artwork_id=1,
            tempo_x100=12450,
            duration_s=301,
            rating=4,
            date_added="2026-09-01",
        ),
        PdbTrack(id=2, title="Second", file_path="/Contents/second.flac", key_id=2),
        PdbTrack(id=3, title="Third", file_path="/Contents/notes.txt", key_id=3),
        PdbTrack(id=4, title="Escapes", file_path="/../outside.mp3"),
        PdbTrack(id=5, title="Linked", file_path="/Contents/link.mp3"),
        PdbTrack(id=6, title="Loose art", file_path="/Contents/second.flac", artwork_id=2),
        PdbTrack(id=7, title="Png art", file_path="/Contents/second.flac", artwork_id=3),
        PdbTrack(
            id=8,
            title="Loose anlz",
            file_path="/Contents/second.flac",
            analyze_path="/Contents/ANLZ0000.DAT",
        ),
        PdbTrack(id=9, title="Nul", file_path="/Contents/bad\x00.mp3"),
        PdbTrack(
            id=10,
            title="Shared dir",
            file_path="/Contents/second.flac",
            analyze_path=f"/{SHARED_ANLZ_DIR}/ANLZ0001.DAT",
        ),
        PdbTrack(
            id=11,
            title="Garbage anlz",
            file_path="/Contents/second.flac",
            analyze_path="/PIONEER/USBANLZ/P002/0000B002/ANLZ0000.DAT",
        ),
        PdbTrack(
            id=12,
            title="Absent anlz",
            file_path="/Contents/second.flac",
            analyze_path="/PIONEER/USBANLZ/P003/0000C003/ANLZ0000.DAT",
        ),
        PdbTrack(
            id=13,
            title="Bad tag",
            file_path="/Contents/second.flac",
            analyze_path="/PIONEER/USBANLZ/P004/0000D004/ANLZ0000.DAT",
        ),
    ]


def synthetic_export(tracks: Sequence[PdbTrack] | None = None) -> PdbExport:
    return PdbExport(
        tracks=synthetic_tracks() if tracks is None else tracks,
        artists={1: " Synth Artist "},
        albums={1: "Synth Album"},
        genres={1: "Techno"},
        keys={1: "6m", 2: "Am", 3: "8A"},
        artwork={
            1: "/PIONEER/Artwork/00001/a1.jpg",
            2: "/Contents/cover.jpg",
            3: "/PIONEER/Artwork/00001/a3.png",
        },
        playlists=[
            PdbPlaylist(id=3, name="Root Set", sort_order=1),
            PdbPlaylist(id=5, name="Folder ", sort_order=0, is_folder=True),
            PdbPlaylist(id=6, name="Child B", parent_id=5, sort_order=2),
            PdbPlaylist(id=7, name="Child A", parent_id=5, sort_order=1),
            PdbPlaylist(id=9, name="Stranded", parent_id=42, sort_order=0),
        ],
        playlist_entries=[(3, 2, 2), (3, 1, 1), (7, 1, 1)],
        history={1: "HISTORY 001"},
        history_entries=[(1, 2, 2), (1, 1, 1)],
    )


def _write(path: Path, data: bytes = AUDIO_BYTES) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_synthetic_stick(volumes_root: Path) -> Path:
    """Build the stick at ``volumes_root/STICK_NAME``; return its mount path.

    ``volumes_root.parent`` gets the host file the symlink row points at.
    """
    root = volumes_root / STICK_NAME
    write_export_pdb(root, synthetic_export())
    _write(root / "Contents" / "Synth Artist" / "First Synthetic .mp3")
    _write(root / "Contents" / "second.flac")
    _write(root / "Contents" / "notes.txt", b"not audio")
    _write(root / "Contents" / "cover.jpg")
    _write(volumes_root / "outside.mp3")
    _write(volumes_root.parent / "host-secret.mp3")
    (root / "Contents" / "link.mp3").symlink_to(volumes_root.parent / "host-secret.mp3")
    _write(root / "PIONEER" / "Artwork" / "00001" / "a1.jpg", ARTWORK_S_BYTES)
    _write(root / "PIONEER" / "Artwork" / "00001" / "a1_m.jpg", ARTWORK_M_BYTES)
    _write(root / "PIONEER" / "Artwork" / "00001" / "a3.png")
    _write_anlz(root)
    _write(root / "Contents" / "ANLZ0000.DAT")
    return root


def _cue_entry(cue: HotCueSpec) -> bytes:
    slot, in_ms, out_ms, color, comment = cue
    return pcp2(slot + 1, in_ms, comment=comment or "", color=color, loop_ms=out_ms)


HotCueSpec = tuple[int, int, int | None, int | None, str | None]


def first_track_ext(hot_cues: Sequence[HotCueSpec] = FIRST_HOT_CUES) -> bytes:
    """Track 1's ANLZ0000.EXT; a test passes other hot cues to edit the stick."""
    return pmai(
        pco2(1, *(_cue_entry(cue) for cue in hot_cues)),
        pco2(0, pcp2(0, FIRST_MEMORY_MS)),
        pwv5([(i * 5) % 32 for i in range(900)]),
        pssi([(1, 1), (9, 2)], end_beat=16, masked=True),
    )


def _write_anlz(root: Path) -> None:
    shared = root / SHARED_ANLZ_DIR
    _write(
        shared / "ANLZ0000.DAT",
        pmai(pqtz(FIRST_GRID), pwav(400, 1), pcob(1, pcpt(1, 152)), pcob(0)),
    )
    _write(shared / "ANLZ0000.EXT", first_track_ext())
    _write(shared / "ANLZ0001.DAT", pmai(pqtz(SHARED_GRID)))
    _write(shared / "ANLZ0001.EXT", pmai(pco2(1, _cue_entry(SHARED_HOT_CUE)), pco2(0)))
    _write(shared / "ANLZ0001.2EX", pmai(pwv6(120, 2), pwv7(600, 2)))
    _write(root / "PIONEER/USBANLZ/P002/0000B002/ANLZ0000.DAT", b"not a PMAI container")
    broken = bytearray(pco2(1, pcp2(2, 2000, color=43)))
    broken[28:32] = (4096).to_bytes(4, "big")  # PCP2 len_entry overruns its tag
    bad_tag = root / "PIONEER/USBANLZ/P004/0000D004"
    _write(
        bad_tag / "ANLZ0000.DAT",
        pmai(pqtz(BAD_TAG_GRID), pcob(1, pcpt(2, BAD_TAG_PCOB_MS)), pcob(0)),
    )
    _write(bad_tag / "ANLZ0000.EXT", pmai(bytes(broken), pco2(0)))
