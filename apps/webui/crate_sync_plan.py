"""Plan-building helpers extracted from :mod:`apps.webui.crate_sync`.

Keeps SQL forks, Rekordbox asset lookup, file accumulation, and owner-manifest
grouping under the complexity ceiling so ``collect_plan`` and
``_rsync_manifest_from_host`` stay coordinators.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

from apps.shared import fs_residency
from apps.webui import crate_sync as cs
from apps.webui.soft_deletes import has_soft_deletes

RbAsset = tuple[Optional[str], Optional[str], Optional[str]]


def selected_track_rows(
    state: sqlite3.Connection,
    wanted: Sequence[str] | None,
) -> list[sqlite3.Row]:
    """Load live track rows, honoring the v6-vs-v7 ``deleted_at`` fork."""
    if wanted is None:
        if has_soft_deletes(state, "tracks"):
            all_tracks_sql = (
                "SELECT stable_id, file_path FROM tracks "
                "WHERE deleted_at IS NULL ORDER BY stable_id"
            )
        else:
            all_tracks_sql = (
                "SELECT stable_id, file_path FROM tracks "
                "ORDER BY stable_id"
            )
        return list(state.execute(all_tracks_sql).fetchall())

    marks = ",".join("?" * len(wanted))
    if has_soft_deletes(state, "tracks"):
        in_list_sql = (
            f"SELECT stable_id, file_path FROM tracks WHERE stable_id IN ({marks}) "
            "AND deleted_at IS NULL ORDER BY stable_id"
        )
    else:
        in_list_sql = (
            f"SELECT stable_id, file_path FROM tracks WHERE stable_id IN ({marks}) "
            "ORDER BY stable_id"
        )
    rows = list(state.execute(in_list_sql, tuple(wanted)).fetchall())
    found = {str(r["stable_id"]) for r in rows}
    missing = [sid for sid in wanted if sid not in found]
    if missing:
        raise RuntimeError(f"stable_id not in state.db: {', '.join(missing)}")
    return rows


def load_rb_assets(master_db: Path | None) -> dict[str, RbAsset]:
    """Index rekordbox content paths by vendor id when master.plain.db exists."""
    rb_assets: dict[str, RbAsset] = {}
    if master_db is None or not master_db.is_file():
        return rb_assets
    master = cs._open_ro(master_db, "MASTER_DB")
    try:
        for row in master.execute(
            "SELECT ID, FolderPath, ImagePath, AnalysisDataPath FROM djmdContent "
            "WHERE rb_local_deleted = 0"
        ):
            rb_assets[str(row["ID"])] = (
                row["FolderPath"] or None,
                row["ImagePath"] or None,
                row["AnalysisDataPath"] or None,
            )
    finally:
        master.close()
    return rb_assets


def add_crate_file(
    files: dict[str, cs.CrateFile],
    source: Path,
    kind: cs.FileKind,
    stable_id: str,
    crate_root: Path,
    user_maps: Sequence[tuple[str, str]],
) -> None:
    dest = cs.crate_dest(source, crate_root=crate_root, user_maps=user_maps)
    key = dest.as_posix()
    size = fs_residency.materialised_size(source)
    if size is None:
        raise RuntimeError(f"source vanished during plan: {source}")
    existing = files.get(key)
    member_ids = tuple(
        sorted({stable_id, *(existing.stable_ids if existing else ())})
    )
    files[key] = cs.CrateFile(
        source=source,
        dest=dest,
        kind=kind,
        size_bytes=size,
        mtime_s=int(source.stat().st_mtime),
        stable_ids=member_ids,
    )


def accumulate_plan_files(
    rows: Sequence[sqlite3.Row],
    vendor_ids: dict[str, str],
    rb_assets: dict[str, RbAsset],
    crate_root: Path,
    user_maps: Sequence[tuple[str, str]],
) -> tuple[dict[str, cs.CrateFile], int, int]:
    files: dict[str, cs.CrateFile] = {}
    skipped_streaming = 0
    skipped_absent = 0
    for row in rows:
        file_path = row["file_path"]
        vendor_id = vendor_ids.get(str(row["stable_id"]))
        folder, image, analysis = (None, None, None)
        if vendor_id is not None:
            folder, image, analysis = rb_assets.get(vendor_id, (None, None, None))
        audio_raw = folder or file_path
        if cs._is_streaming(audio_raw):
            skipped_streaming += 1
            continue
        audio = cs._materialised_source(audio_raw)
        if audio is None:
            skipped_absent += 1
            continue
        stable_id = str(row["stable_id"])
        add_crate_file(files, audio, "audio", stable_id, crate_root, user_maps)
        for anlz in cs._anlz_sources(analysis):
            add_crate_file(files, anlz, "anlz", stable_id, crate_root, user_maps)
        for art in cs._artwork_sources(image):
            add_crate_file(files, art, "artwork", stable_id, crate_root, user_maps)
    return files, skipped_streaming, skipped_absent


def group_manifest_relatives(
    manifest: dict[str, object], crate_root: Path
) -> dict[tuple[Path, Path], list[Path]]:
    """Validate owner-manifest entries and group relatives by source/dest roots."""
    groups: dict[tuple[Path, Path], list[Path]] = {}
    for entry in cs._manifest_files(manifest):
        source = Path(str(entry["source"]))
        source_root = Path(str(entry["source_root"]))
        dest = Path(str(entry["dest"]))
        dest_root = Path(str(entry["dest_root"]))
        relative = Path(str(entry["relative"]))
        if not (source_root.is_absolute() and dest_root.is_absolute()):
            raise RuntimeError("owner manifest roots must be absolute")
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"unsafe owner relative path: {relative}")
        if source_root / relative != source:
            raise RuntimeError(f"owner source mapping drift: {source}")
        if dest_root / relative != dest:
            raise RuntimeError(f"owner destination mapping drift: {dest}")
        # This lane writes LOCALLY (rsync pulls from the owner into the crate),
        # so both sides resolve: `..` collapses and a symlink aimed back at the
        # Pioneer share is followed rather than trusted.
        cs._assert_within_crate(dest, crate_root, local=True)
        cs._assert_within_crate(dest_root, crate_root, local=True)
        groups.setdefault((source_root, dest_root), []).append(relative)
    return groups
