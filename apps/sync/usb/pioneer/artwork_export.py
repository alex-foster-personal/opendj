"""Track artwork step of the classic USB export (USBEXP-10048, issue #10048).

Writes each track's cover at the two sizes a player and the stick browser
read, in the rekordbox stick layout (mimicked, no review round):
``PIONEER/Artwork/<nnnnn>/a<id>.jpg`` (80 px) and ``a<id>_m.jpg`` (240 px).
The pdb artwork row's path names the small file; the ``_m`` sibling is the
medium one (``stick_library._artwork_file`` resolves the same rule).

The core (:func:`plan_artwork`) is pure: it returns the pdb artwork table,
each track's artwork id and the file hash list. :func:`write_artwork` puts
the files on the stick and :func:`verify_artwork` is the read-back. The
classic export (#10040) and the pdb writer (#10031) consume this plan.

Requirements (mini-PRD):
  plan
  [if] a covered track gets no row, or a row without both sizes [then ⛔️]
  [if] two tracks with byte-identical covers get two rows or two pairs of files [then ⛔️]
  [if] a track without a cover gets an id other than 0 or any file [then ⛔️]
  render
  [if] a small file's longest edge is not 80 px for a 300 px source [then ⛔️]
  [if] a medium file's longest edge is not 240 px for a 300 px source [then ⛔️]
  [if] an undecodable cover is silently dropped to id 0 instead of refused [then ⛔️]
  write and read-back
  [if] an existing file on the stick is overwritten [then ⛔️]
  [if] an artwork file is missing from the plan's hash list [then ⛔️]
  [if] a missing or altered file passes verify_artwork [then ⛔️]
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import HTTPException

from apps.adapters.rekordbox import paths as rb_paths
from apps.shared.artwork_resize import ARTWORK_EDGES, render
from apps.shared.hashing import sha256_file_hex

ARTWORK_DIR = PurePosixPath("PIONEER", "Artwork")
ARTWORK_BUCKET_SIZE = 1000  # ids per <nnnnn> folder; mimicked from the rekordbox stick layout

ArtworkSource = Callable[[str], bytes | None]  # stable_id -> original cover bytes, None = no cover


class ArtworkExportError(RuntimeError):
    """A refusal with a stable ``code`` (same shape as ``UsbExportError``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ArtworkFile:
    relative_path: str  # volume-relative POSIX, no leading slash
    data: bytes
    sha256: str


@dataclass(frozen=True)
class ArtworkRow:
    artwork_id: int
    pdb_path: str  # "/PIONEER/Artwork/00001/a1.jpg"
    source_sha256: str  # hash of the original cover bytes (dedupe key)
    small: ArtworkFile
    medium: ArtworkFile


@dataclass(frozen=True)
class ArtworkPlan:
    rows: tuple[ArtworkRow, ...]
    track_artwork_ids: Mapping[str, int]  # every requested stable_id; 0 = no artwork

    def pdb_table(self) -> dict[int, str]:
        return {row.artwork_id: row.pdb_path for row in self.rows}

    def files(self) -> tuple[ArtworkFile, ...]:
        return tuple(f for row in self.rows for f in (row.small, row.medium))

    def file_hashes(self) -> dict[str, str]:
        return {f.relative_path: f.sha256 for f in self.files()}

    def to_payload(self) -> dict[str, Any]:
        return {
            "rows": [
                {
                    "artwork_id": row.artwork_id,
                    "pdb_path": row.pdb_path,
                    "source_sha256": row.source_sha256,
                    "small": row.small.relative_path,
                    "medium": row.medium.relative_path,
                }
                for row in self.rows
            ],
            "track_artwork_ids": dict(self.track_artwork_ids),
            "pdb_table": {str(k): v for k, v in self.pdb_table().items()},
            "file_hashes": self.file_hashes(),
        }


# ----- paths -----------------------------------------------------------------


def artwork_pdb_path(artwork_id: int) -> str:
    """The pdb row path of the small file for ``artwork_id`` (1-based)."""
    if artwork_id < 1:
        raise ValueError(f"artwork_id must be >= 1, got {artwork_id}")
    bucket = 1 + (artwork_id - 1) // ARTWORK_BUCKET_SIZE
    return f"/{ARTWORK_DIR}/{bucket:05d}/a{artwork_id}.jpg"


def medium_relative_path(pdb_path: str) -> str:
    """Volume-relative path of the ``_m`` sibling (stick_library's rule)."""
    pure = PurePosixPath(pdb_path.lstrip("/"))
    return str(pure.with_name(f"{pure.stem}_m{pure.suffix}"))


def _artwork_file(relative_path: str, data: bytes) -> ArtworkFile:
    return ArtworkFile(relative_path, data, hashlib.sha256(data).hexdigest())


# ----- plan (pure) -----------------------------------------------------------


def plan_artwork(stable_ids: Sequence[str], source: ArtworkSource) -> ArtworkPlan:
    """Assign artwork rows (deduped on the original cover's sha256) and render both sizes."""
    rows: list[ArtworkRow] = []
    by_hash: dict[str, int] = {}
    track_ids: dict[str, int] = {}
    for sid in stable_ids:
        if sid in track_ids:
            continue
        data = source(sid)
        if data is None:
            track_ids[sid] = 0
            continue
        digest = hashlib.sha256(data).hexdigest()
        if digest in by_hash:
            track_ids[sid] = by_hash[digest]
            continue
        artwork_id = len(rows) + 1
        rows.append(_render_row(sid, artwork_id, digest, data))
        by_hash[digest] = artwork_id
        track_ids[sid] = artwork_id
    return ArtworkPlan(rows=tuple(rows), track_artwork_ids=track_ids)


def _render_row(sid: str, artwork_id: int, digest: str, data: bytes) -> ArtworkRow:
    small = render(data, ARTWORK_EDGES["s"])
    medium = render(data, ARTWORK_EDGES["m"])
    if small is None or medium is None:
        raise ArtworkExportError("artwork_undecodable", f"track {sid}: cover bytes are not an image Pillow can decode")
    pdb_path = artwork_pdb_path(artwork_id)
    return ArtworkRow(
        artwork_id=artwork_id,
        pdb_path=pdb_path,
        source_sha256=digest,
        small=_artwork_file(pdb_path.lstrip("/"), small),
        medium=_artwork_file(medium_relative_path(pdb_path), medium),
    )


# ----- write and read-back ---------------------------------------------------


def write_artwork(plan: ArtworkPlan, volume_root: Path) -> dict[str, str]:
    """Write every planned file (exclusive create, fsync); return on-disk hashes."""
    written: dict[str, str] = {}
    for f in plan.files():
        target = volume_root / f.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as fh:
                fh.write(f.data)
                fh.flush()
                os.fsync(fh.fileno())
        except FileExistsError as exc:
            raise ArtworkExportError("artwork_target_exists", f.relative_path) from exc
        written[f.relative_path] = sha256_file_hex(target)
    return written


def verify_artwork(plan: ArtworkPlan, volume_root: Path) -> None:
    """Re-hash every planned file on the stick; refuse a missing or altered one."""
    for f in plan.files():
        target = volume_root / f.relative_path
        if not target.is_file():
            raise ArtworkExportError("readback_missing", f.relative_path)
        if sha256_file_hex(target) != f.sha256:
            raise ArtworkExportError("readback_hash_mismatch", f.relative_path)


# ----- sources ---------------------------------------------------------------


def library_artwork_source(*, online: bool = False) -> ArtworkSource:
    """The library's artwork store (``local_artwork`` at ``orig``); None when it has no cover."""

    def source(stable_id: str) -> bytes | None:
        try:
            data, _mime = rb_paths.local_artwork(stable_id, online=online, size="orig")
        except HTTPException as exc:
            detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
            if exc.status_code == 404 and detail.get("code") == "ARTWORK_NOT_FOUND":
                return None
            raise
        return data

    return source


def file_source(covers: Mapping[str, Path]) -> ArtworkSource:
    """Covers supplied as files (agent path); tracks not in ``covers`` have none."""

    def source(stable_id: str) -> bytes | None:
        path = covers.get(stable_id)
        return path.read_bytes() if path is not None else None

    return source


__all__ = [
    "ARTWORK_BUCKET_SIZE",
    "ARTWORK_DIR",
    "ArtworkExportError",
    "ArtworkFile",
    "ArtworkPlan",
    "ArtworkRow",
    "ArtworkSource",
    "artwork_pdb_path",
    "file_source",
    "library_artwork_source",
    "medium_relative_path",
    "plan_artwork",
    "verify_artwork",
    "write_artwork",
]
