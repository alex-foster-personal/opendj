"""SPIKE-B2 disposable data-dir builder for live demucs PVDI acceptance (#274).

Never reads or writes the worktree canonical ``data/state/vocal-cache`` or live
``state.db`` / ``master.plain.db``. Source audio and ANLZ are read-only from a
real library pointed at by ``VOCALS_ACCEPTANCE_SOURCE_DATA_DIR`` (or fallbacks).

Regression one-liners:
  - if Dub Congas demucs coverage > 5% then broken
  - if Mozart's House demucs IoU < 0.6 vs PVDI then broken
  - if trickle writes outside the disposable vocal-cache dir then broken
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.shared.paths import DATA_DIR
from apps.shared.platform_paths import PathMap, load_path_map, resolve_asset_path
from apps.vocals.cli import pvdi_present
from apps.webui.server.rb_vendor_pkg.anlz import _vocal_regions, read_pvdi

_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "vocals" / "spike-b2-acceptance.json"
)
_PVDI_FIXED_HEADER = bytes.fromhex("0000040056220001")
_PLAYLIST_ID = "spike-b2-acceptance"
_USE_SOURCE_PATHS_ENV = "VOCALS_ACCEPTANCE_USE_SOURCE_PATHS"


@dataclass(frozen=True)
class SpikeB2Track:
    vendor_id: str
    slug: str
    role: str
    stable_id: str
    audio_path: Path
    analysis_dat_path: Path
    twoex_path: Path
    length_s: int
    max_coverage_pct: float | None = None
    min_pvdi_iou: float | None = None


@dataclass(frozen=True)
class SpikeB2Fixture:
    data_dir: Path
    playlist_id: str
    tracks: tuple[SpikeB2Track, ...]
    manifest_path: Path


def load_manifest() -> dict[str, Any]:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def resolve_source_data_dir() -> Path | None:
    """Resolution order from the issue #274 plan."""
    candidates: list[Path] = []
    for env_name in (
        load_manifest()["source_data_dir_env"],
        "VOCALS_DATA_DIR",
    ):
        raw = os.environ.get(env_name)
        if raw:
            candidates.append(Path(raw))
    data_dir_env = os.environ.get("DATA_DIR")
    if data_dir_env:
        candidates.append(Path(data_dir_env))
    candidates.append(DATA_DIR)
    for candidate in candidates:
        if _is_usable_source(candidate):
            return candidate
    return None


def _is_usable_source(path: Path) -> bool:
    state = path / "state" / "state.db"
    master = path / "master.plain.db"
    return (
        state.is_file()
        and master.is_file()
        and state.stat().st_size > 0
        and master.stat().st_size > 0
    )


def assert_disposable(cache_dir: Path, canonical_data_dir: Path) -> None:
    """Fail fast when ``cache_dir`` resolves inside canonical vocal-cache."""
    cache_resolved = cache_dir.resolve()
    canonical_cache = (canonical_data_dir / "state" / "vocal-cache").resolve()
    if cache_resolved == canonical_cache or canonical_cache in cache_resolved.parents:
        raise ValueError(
            f"refusing disposable vocal-cache inside canonical tree: {cache_resolved}"
        )


def _empty_2ex() -> bytes:
    head = b"PMAI" + struct.pack(">II", 28, 28)
    return head + b"\x00" * (28 - len(head))


def _link_or_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def _lookup_vendor_track(source_data_dir: Path, vendor_id: str) -> dict[str, Any]:
    state_path = source_data_dir / "state" / "state.db"
    master_path = source_data_dir / "master.plain.db"
    state = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
    try:
        row = state.execute(
            "SELECT t.stable_id, COALESCE(t.title, '') "
            "FROM tracks t "
            "JOIN track_vendor_ids v ON v.stable_id = t.stable_id "
            "WHERE v.vendor = 'rekordbox' AND v.vendor_id = ? "
            "AND t.deleted_at IS NULL",
            (vendor_id,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(
                f"SPIKE-B2 vendor_id {vendor_id} not found in {state_path}"
            )
        stable_id, title = row
    finally:
        state.close()

    master = sqlite3.connect(f"file:{master_path}?mode=ro", uri=True)
    try:
        content = master.execute(
            "SELECT ID, Title, Length, FolderPath, AnalysisDataPath "
            "FROM djmdContent WHERE ID = ? AND rb_local_deleted = 0",
            (vendor_id,),
        ).fetchone()
        if content is None:
            raise FileNotFoundError(
                f"SPIKE-B2 vendor_id {vendor_id} missing from {master_path}"
            )
        _, rb_title, length_s, folder_path, adp = content
    finally:
        master.close()

    if not folder_path or not adp:
        raise FileNotFoundError(
            f"SPIKE-B2 vendor_id {vendor_id} lacks FolderPath or AnalysisDataPath"
        )
    # Real rekordbox rows store AnalysisDataPath share-relative
    # ("/PIONEER/USBANLZ/..."), so resolve both paths the way production
    # (apps.vocals.cli.load_tracks / _anlz_data_file) does.
    path_map = load_path_map(source_data_dir)
    audio_path = _resolved_or_raise(str(folder_path), path_map, vendor_id, "audio")
    if not audio_path.is_file():
        raise FileNotFoundError(f"SPIKE-B2 audio missing for {vendor_id}: {audio_path}")
    dat_path = _resolved_or_raise(str(adp), path_map, vendor_id, "ANLZ .DAT")
    if not dat_path.is_file():
        raise FileNotFoundError(f"SPIKE-B2 ANLZ .DAT missing for {vendor_id}: {dat_path}")
    twoex_path = dat_path.with_suffix(".2EX")
    if not twoex_path.is_file():
        raise FileNotFoundError(f"SPIKE-B2 ANLZ .2EX missing for {vendor_id}: {twoex_path}")
    return {
        "stable_id": str(stable_id),
        "title": str(rb_title or title),
        "length_s": int(length_s or 0),
        "audio_path": audio_path,
        "dat_path": dat_path,
        "twoex_path": twoex_path,
    }


def _resolved_or_raise(raw: str, path_map: PathMap, vendor_id: str, what: str) -> Path:
    mapped = resolve_asset_path(raw, path_map=path_map)
    if mapped.resolved is None:
        raise FileNotFoundError(
            f"SPIKE-B2 {what} path unresolvable for {vendor_id}: {raw} ({mapped.reason})"
        )
    return mapped.resolved


def pvdi_regions(twoex_path: Path) -> list[dict[str, float]]:
    decoded = read_pvdi(twoex_path)
    if decoded is None:
        return []
    fps, envelope = decoded
    return [
        {"start_s": float(item["start_s"]), "end_s": float(item["end_s"])}
        for item in _vocal_regions(envelope, fps)
    ]


def build_disposable_data_dir(dest: Path, source_data_dir: Path) -> SpikeB2Fixture:
    """Build a writable disposable root with the two SPIKE-B2 tracks."""
    manifest = load_manifest()
    dest = dest.resolve()
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "state").mkdir(parents=True)
    (dest / "state" / "vocal-cache").mkdir(parents=True)
    media_dir = dest / "media"
    media_dir.mkdir()

    use_source_paths = os.environ.get(_USE_SOURCE_PATHS_ENV) == "1"
    built_tracks: list[SpikeB2Track] = []

    state = sqlite3.connect(dest / "state" / "state.db")
    master = sqlite3.connect(dest / "master.plain.db")
    state.executescript(
        """
        CREATE TABLE tracks (
            stable_id TEXT PRIMARY KEY, title TEXT, deleted_at TEXT);
        CREATE TABLE track_vendor_ids (
            stable_id TEXT, vendor TEXT, vendor_id TEXT);
        CREATE TABLE playlists (
            playlist_id TEXT PRIMARY KEY, name TEXT, deleted_at TEXT);
        CREATE TABLE playlist_memberships (
            playlist_id TEXT, stable_id TEXT, position INTEGER,
            deleted_at TEXT);
        """
    )
    master.execute(
        "CREATE TABLE djmdContent (ID TEXT, Title TEXT, Length INTEGER, "
        "FolderPath TEXT, AnalysisDataPath TEXT, rb_local_deleted INTEGER)"
    )

    for pos, spec in enumerate(manifest["tracks"]):
        vendor_id = str(spec["vendor_id"])
        source = _lookup_vendor_track(source_data_dir, vendor_id)
        slug = str(spec["slug"])
        if spec["role"] == "vocal" and not pvdi_present(source["twoex_path"]):
            raise RuntimeError(
                f"SPIKE-B2 track {slug} requires PVDI in .2EX: {source['twoex_path']}"
            )

        if use_source_paths:
            folder_path = str(source["audio_path"])
            adp = str(source["dat_path"])
            audio_path = source["audio_path"]
            dat_path = source["dat_path"]
            twoex_path = source["twoex_path"]
        else:
            audio_dest = media_dir / f"{slug}{source['audio_path'].suffix.lower()}"
            dat_dest = media_dir / f"{slug}.DAT"
            twoex_dest = media_dir / f"{slug}.2EX"
            _link_or_copy(source["audio_path"], audio_dest)
            _link_or_copy(source["dat_path"], dat_dest)
            _link_or_copy(source["twoex_path"], twoex_dest)
            folder_path = str(audio_dest)
            adp = str(dat_dest)
            audio_path = audio_dest
            dat_path = dat_dest
            twoex_path = twoex_dest

        stable_id = str(source["stable_id"])
        state.execute(
            "INSERT INTO tracks (stable_id, title) VALUES (?, ?)",
            (stable_id, source["title"]),
        )
        state.execute(
            "INSERT INTO track_vendor_ids VALUES (?, 'rekordbox', ?)",
            (stable_id, vendor_id),
        )
        master.execute(
            "INSERT INTO djmdContent VALUES (?, ?, ?, ?, ?, 0)",
            (vendor_id, source["title"], source["length_s"], folder_path, adp),
        )
        state.execute(
            "INSERT INTO playlist_memberships (playlist_id, stable_id, position) "
            "VALUES (?, ?, ?)",
            (_PLAYLIST_ID, stable_id, pos),
        )
        built_tracks.append(
            SpikeB2Track(
                vendor_id=vendor_id,
                slug=slug,
                role=str(spec["role"]),
                stable_id=stable_id,
                audio_path=audio_path,
                analysis_dat_path=dat_path,
                twoex_path=twoex_path,
                length_s=int(source["length_s"]),
                max_coverage_pct=spec.get("max_coverage_pct"),
                min_pvdi_iou=spec.get("min_pvdi_iou"),
            )
        )

    state.execute(
        "INSERT INTO playlists (playlist_id, name) VALUES (?, ?)",
        (_PLAYLIST_ID, "SPIKE-B2 acceptance"),
    )
    state.commit()
    state.close()
    master.commit()
    master.close()

    assert_disposable(dest / "state" / "vocal-cache", DATA_DIR)
    return SpikeB2Fixture(
        data_dir=dest,
        playlist_id=_PLAYLIST_ID,
        tracks=tuple(built_tracks),
        manifest_path=_MANIFEST_PATH,
    )
