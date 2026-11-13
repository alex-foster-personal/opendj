"""Classic rekordbox device library export: plan, apply and read-back receipts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from apps.shared.hashing import sha256_file, sha256_file_hex
from apps.sync.usb.copy import _fsync_path
from apps.sync.usb.layout import sanitise_segment
from apps.sync.usb.pioneer.anlz_writer import TrackAnlzInput, analyze_paths, build_dat_ext, write_track
from apps.sync.usb.pioneer.pdb_writer import PdbExport, PdbPlaylist, PdbTrack, write_export_pdb
from apps.sync.usb.pioneer.reader import read_usb_export, validate_invariants


class ClassicExportError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ClassicTrackInput:
    source_path: Path
    title: str
    artist: str
    album: str
    bpm: float
    key_name: str
    duration_s: int = 120
    analysis: TrackAnlzInput | None = None


@dataclass(frozen=True)
class ClassicPlaylistInput:
    name: str
    pdb_track_ids: tuple[int, ...]


@dataclass(frozen=True)
class PlannedFile:
    destination: str
    source_path: str | None
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ClassicPlan:
    plan_id: str
    tracks: tuple[dict[str, Any], ...]
    playlists: tuple[ClassicPlaylistInput, ...]
    files: tuple[PlannedFile, ...]
    sources: tuple[tuple[str, str], ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "tracks": list(self.tracks),
            "playlists": [{"name": p.name, "pdb_track_ids": list(p.pdb_track_ids)} for p in self.playlists],
            "files": [
                {
                    "destination": f.destination,
                    "source_path": f.source_path,
                    "sha256": f.sha256,
                    "size_bytes": f.size_bytes,
                }
                for f in self.files
            ],
            "sources": [list(pair) for pair in self.sources],
        }

    @staticmethod
    def from_payload(data: dict[str, Any]) -> ClassicPlan:
        return ClassicPlan(
            plan_id=str(data["plan_id"]),
            tracks=tuple(data["tracks"]),
            playlists=tuple(
                ClassicPlaylistInput(name=p["name"], pdb_track_ids=tuple(p["pdb_track_ids"])) for p in data["playlists"]
            ),
            files=tuple(
                PlannedFile(
                    destination=f["destination"],
                    source_path=f.get("source_path"),
                    sha256=f["sha256"],
                    size_bytes=int(f["size_bytes"]),
                )
                for f in data["files"]
            ),
            sources=tuple((str(s), str(h)) for s, h in data["sources"]),
        )


def _audio_dest(artist: str, album: str, title: str, suffix: str) -> PurePosixPath:
    a = sanitise_segment(artist or "Unknown Artist")
    b = sanitise_segment(album or "Unknown Album")
    t = sanitise_segment(title or "Track")
    return PurePosixPath("Contents") / a / b / (t + suffix)


def _key_table(keys: set[str]) -> dict[int, str]:
    return {index: name for index, name in enumerate(sorted(keys), start=1)}


def _analysis_dict(analysis: TrackAnlzInput) -> dict[str, Any]:
    return asdict(analysis)


def _analysis_from_row(row: dict[str, Any]) -> TrackAnlzInput:
    raw = row.get("analysis")
    if isinstance(raw, TrackAnlzInput):
        return raw
    if isinstance(raw, dict):
        return TrackAnlzInput(**raw)
    return TrackAnlzInput(bpm=float(row["bpm"]), seed=int(row["pdb_id"]))


def plan_classic_export(
    tracks: list[ClassicTrackInput],
    playlists: list[ClassicPlaylistInput],
) -> ClassicPlan:
    if not tracks:
        raise ClassicExportError("no_tracks", "at least one track is required")
    track_rows: list[dict[str, Any]] = []
    files: list[PlannedFile] = []
    sources: list[tuple[str, str]] = []

    for index, track in enumerate(tracks, start=1):
        if not track.source_path.is_file():
            raise ClassicExportError("source_missing", f"source not found: {track.source_path}")
        digest = sha256_file(track.source_path)
        bare = sha256_file_hex(track.source_path)
        sources.append((str(track.source_path.resolve()), digest))
        suffix = track.source_path.suffix.lower() or ".wav"
        dest_rel = _audio_dest(track.artist, track.album, track.title, suffix)
        stick_path = "/" + dest_rel.as_posix()
        analysis = track.analysis or TrackAnlzInput(bpm=track.bpm, seed=index)
        rel_anlz, analyze_path = analyze_paths(pdb_track_id=index, content_sha256_hex=bare)
        files.append(
            PlannedFile(
                destination=stick_path,
                source_path=str(track.source_path.resolve()),
                sha256=digest,
                size_bytes=track.source_path.stat().st_size,
            )
        )
        files.append(
            PlannedFile(
                destination=f"/{rel_anlz}",
                source_path=None,
                sha256="",
                size_bytes=0,
            )
        )
        ext_rel = PurePosixPath(rel_anlz).with_suffix(".EXT").as_posix()
        files.append(
            PlannedFile(
                destination=f"/{ext_rel}",
                source_path=None,
                sha256="",
                size_bytes=0,
            )
        )
        track_rows.append(
            {
                "pdb_id": index,
                "title": track.title,
                "artist": track.artist,
                "album": track.album,
                "bpm": track.bpm,
                "key_name": track.key_name,
                "duration_s": track.duration_s,
                "analyze_path": analyze_path,
                "file_path": stick_path,
                "rel_anlz": rel_anlz,
                "analysis": _analysis_dict(analysis),
            }
        )

    files.append(
        PlannedFile(
            destination="/PIONEER/rekordbox/export.pdb",
            source_path=None,
            sha256="",
            size_bytes=0,
        )
    )

    payload = json.dumps(
        {
            "sources": sources,
            "track_ids": [t["pdb_id"] for t in track_rows],
            "playlists": [(p.name, list(p.pdb_track_ids)) for p in playlists],
        },
        sort_keys=True,
    )
    plan_id = hashlib.sha256(payload.encode()).hexdigest()

    return ClassicPlan(
        plan_id=plan_id,
        tracks=tuple(track_rows),
        playlists=tuple(playlists),
        files=tuple(files),
        sources=tuple(sources),
    )


def _require_empty_target(target: Path) -> None:
    target = target.resolve()
    if target.exists():
        if any(target.iterdir()):
            raise ClassicExportError("target_not_empty", f"target directory is not empty: {target}")
    else:
        target.mkdir(parents=True)


def _verify_sources(plan: ClassicPlan) -> None:
    for path_s, expected in plan.sources:
        path = Path(path_s)
        if not path.is_file():
            raise ClassicExportError("source_missing", f"source not found: {path}")
        current = sha256_file(path)
        if current != expected:
            raise ClassicExportError(
                "source_changed",
                f"source hash mismatch for {path}: plan has {expected}, disk has {current}",
            )


def _copy_audio(src: Path, dst: Path, *, expected_hash: str, expected_size: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_suffix(dst.suffix + ".part")
    shutil.copy2(src, part)
    _fsync_path(part)
    os.replace(part, dst)
    if dst.stat().st_size != expected_size:
        raise ClassicExportError("size_mismatch", f"size mismatch after copy: {dst}")
    if sha256_file(dst) != expected_hash:
        raise ClassicExportError("hash_mismatch", f"hash mismatch after copy: {dst}")


def apply_classic_export(plan: ClassicPlan, target: Path) -> dict[str, Any]:
    _require_empty_target(target)
    _verify_sources(plan)

    keys = _key_table({str(t["key_name"]) for t in plan.tracks})
    artists: dict[int, str] = {}
    albums: dict[int, str] = {}
    artist_ids: dict[str, int] = {}
    album_ids: dict[str, int] = {}
    next_lookup = 1
    pdb_tracks: list[PdbTrack] = []
    playlist_entries: list[tuple[int, int, int]] = []
    pdb_playlists: list[PdbPlaylist] = [PdbPlaylist(id=1, name="ROOT", is_folder=True)]
    next_playlist_id = 2

    for pl in plan.playlists:
        pdb_playlists.append(PdbPlaylist(id=next_playlist_id, name=pl.name, parent_id=1))
        for entry_index, track_id in enumerate(pl.pdb_track_ids, start=1):
            playlist_entries.append((next_playlist_id, track_id, entry_index))
        next_playlist_id += 1

    file_by_dest = {f.destination: f for f in plan.files if f.source_path}

    for row in sorted(plan.tracks, key=lambda t: int(t["pdb_id"])):
        track_id = int(row["pdb_id"])
        artist_name = str(row["artist"])
        album_name = str(row["album"])
        if artist_name not in artist_ids:
            artist_ids[artist_name] = next_lookup
            artists[next_lookup] = artist_name
            next_lookup += 1
        if album_name not in album_ids:
            album_ids[album_name] = next_lookup
            albums[next_lookup] = album_name
            next_lookup += 1

        analysis = _analysis_from_row(row)
        dat, ext = build_dat_ext(analysis)
        rel_anlz = str(row["rel_anlz"])
        write_track(target, rel_anlz, dat=dat, ext=ext)

        src_path = Path(plan.sources[track_id - 1][0])
        dest = target / str(row["file_path"]).lstrip("/")
        file_spec = file_by_dest[row["file_path"]]
        _copy_audio(src_path, dest, expected_hash=file_spec.sha256, expected_size=file_spec.size_bytes)

        pdb_tracks.append(
            PdbTrack(
                id=track_id,
                title=str(row["title"]),
                file_path=str(row["file_path"]),
                analyze_path=str(row["analyze_path"]),
                artist_id=artist_ids[artist_name],
                album_id=album_ids[album_name],
                genre_id=1,
                key_id=next((kid for kid, name in keys.items() if name == row["key_name"]), 1),
                tempo_x100=round(float(row["bpm"]) * 100),
                duration_s=int(row.get("duration_s", 120)),
                date_added="2026-11-09",
            )
        )

    export = PdbExport(
        tracks=pdb_tracks,
        artists=artists,
        albums=albums,
        genres={1: "Unknown"},
        keys=keys,
        playlists=pdb_playlists,
        playlist_entries=playlist_entries,
    )
    write_export_pdb(target, export)
    return {
        "plan_id": plan.plan_id,
        "target": str(target.resolve()),
        "files_written": len(plan.files),
    }


def readback_classic_export(plan: ClassicPlan, target: Path) -> dict[str, Any]:
    target = target.resolve()
    for planned in plan.files:
        if not planned.sha256 and planned.size_bytes == 0:
            continue
        dest = target / planned.destination.lstrip("/")
        if not dest.is_file():
            return {
                "verified": False,
                "mismatch": {"destination": planned.destination, "reason": "missing"},
                "plan_id": plan.plan_id,
            }
        size = dest.stat().st_size
        if planned.size_bytes and size != planned.size_bytes:
            return {
                "verified": False,
                "mismatch": {
                    "destination": planned.destination,
                    "reason": "size",
                    "expected": planned.size_bytes,
                    "actual": size,
                },
                "plan_id": plan.plan_id,
            }
        if planned.sha256:
            actual = sha256_file(dest)
            if actual != planned.sha256:
                return {
                    "verified": False,
                    "mismatch": {
                        "destination": planned.destination,
                        "reason": "hash",
                        "expected": planned.sha256,
                        "actual": actual,
                    },
                    "plan_id": plan.plan_id,
                }
    return {"verified": True, "plan_id": plan.plan_id}


__all__ = [
    "ClassicExportError",
    "ClassicPlan",
    "ClassicPlaylistInput",
    "ClassicTrackInput",
    "apply_classic_export",
    "plan_classic_export",
    "readback_classic_export",
    "read_usb_export",
    "validate_invariants",
]
