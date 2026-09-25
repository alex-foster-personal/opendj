"""A synthetic Play from USB stick for tests: one ``export.pdb`` from
``export_pdb_builder`` plus the files its rows point at, including rows that
must be REFUSED (a ``..`` escape, a symlink out of the mount, a non-audio
file, artwork outside PIONEER/Artwork, a NUL byte). All names are invented;
nothing is copied from a real stick.
"""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from tests.sync.usb.export_pdb_builder import PdbExport, PdbPlaylist, PdbTrack, write_export_pdb

STICK_UUID = "0A1B2C3D-4E5F-4061-8293-A4B5C6D7E8F9"
OTHER_UUID = "11111111-2222-4333-8444-555555555555"
STICK_NAME = "SYN STICK "  # trailing space on purpose, like the real test stick
AUDIO_BYTES = b"\xff\xfb\x90\x00synthetic audio payload"
ARTWORK_S_BYTES = b"small"
ARTWORK_M_BYTES = b"medium"


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
    for suffix in (".DAT", ".EXT"):
        _write(root / "PIONEER" / "USBANLZ" / "P001" / "0000A001" / f"ANLZ0000{suffix}")
    _write(root / "Contents" / "ANLZ0000.DAT")
    return root
