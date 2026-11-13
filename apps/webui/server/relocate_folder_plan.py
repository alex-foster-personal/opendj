"""Folder-level relocate planner (LIBM-72): one old-prefix to new-prefix decision, previewed first.

rekordbox and Serato both bulk-rewrite paths with no preview. Here the planner turns the library
rows plus two folder prefixes into a per-row plan the user reads BEFORE anything is written.

Pure apart from the ``exists`` callable it is given: no database, no writes.

* Only MISSING tracks are planned: a row whose file still exists at its recorded path is left
  alone, so the plan can never break a working track.
* Prefixes match whole path components: ``/Music/A`` matches ``/Music/A/x.mp3`` but never
  ``/Music/AB/x.mp3``.
* ``plan_id`` is a digest of the prefixes and every planned row (path, version, target, whether
  the target exists). Apply re-plans and refuses when the digest differs, so a library that
  changed since the preview is never written from a stale plan.

Nearby missing files (LIBM-73, Lightroom's "find nearby missing" relink): once ONE track is found
again in another folder, :func:`plan_nearby` proposes that same folder move for every missing
track under its old folder. It is the folder plan for (old folder, new folder), so its
``plan_id`` applies through the folder apply unchanged.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import PurePath


@dataclass(frozen=True)
class RelocateSourceRow:
    """One library track as the planner reads it."""

    stable_id: str
    file_path: str
    version: str


@dataclass(frozen=True)
class RelocatePlanRow:
    """One planned rewrite: ``old_path`` to ``new_path``; ``new_exists`` says whether apply will write it."""

    stable_id: str
    old_path: str
    new_path: str
    new_exists: bool
    version: str


@dataclass(frozen=True)
class RelocatePlan:
    """The whole preview: its digest and every planned row, sorted by old path."""

    plan_id: str
    from_prefix: str
    to_prefix: str
    rows: tuple[RelocatePlanRow, ...]

    @property
    def writable(self) -> tuple[RelocatePlanRow, ...]:
        """The rows apply rewrites: the new file exists."""
        return tuple(row for row in self.rows if row.new_exists)

    @property
    def skipped(self) -> tuple[RelocatePlanRow, ...]:
        """The rows apply leaves alone and reports: no file at the new path."""
        return tuple(row for row in self.rows if not row.new_exists)


class InvalidRelocatePrefix(ValueError):
    """A prefix is empty, relative, or the two prefixes are the same folder."""


@dataclass(frozen=True)
class LocatedTrack:
    """A track found again: the path it was recorded at before, and the path it is at now."""

    stable_id: str
    previous_path: str
    current_path: str


class NotAFolderMove(ValueError):
    """The located track stayed in the same folder, so there is no folder move to propose."""


def _prefix(raw: str, name: str) -> PurePath:
    if not raw.strip():
        raise InvalidRelocatePrefix(f"{name} must be a non-empty folder path")
    path = PurePath(raw)
    if not path.is_absolute():
        raise InvalidRelocatePrefix(f"{name} must be an absolute folder path, got {raw!r}")
    return path


def _plan_id(from_prefix: str, to_prefix: str, rows: tuple[RelocatePlanRow, ...]) -> str:
    payload = json.dumps(
        {"from": from_prefix, "to": to_prefix, "rows": [asdict(row) for row in rows]},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_folder_relocate(
    rows: Iterable[RelocateSourceRow],
    *,
    from_prefix: str,
    to_prefix: str,
    exists: Callable[[str], bool],
) -> RelocatePlan:
    """Plan every missing track under ``from_prefix`` onto the same relative path under ``to_prefix``."""
    old_root = _prefix(from_prefix, "from_prefix")
    new_root = _prefix(to_prefix, "to_prefix")
    if old_root == new_root:
        raise InvalidRelocatePrefix("from_prefix and to_prefix name the same folder")
    planned: list[RelocatePlanRow] = []
    for row in rows:
        old_path = PurePath(row.file_path)
        if old_path == old_root or not old_path.is_relative_to(old_root):
            continue
        if exists(row.file_path):
            continue
        new_path = str(new_root / old_path.relative_to(old_root))
        planned.append(
            RelocatePlanRow(
                stable_id=row.stable_id,
                old_path=row.file_path,
                new_path=new_path,
                new_exists=exists(new_path),
                version=row.version,
            )
        )
    ordered = tuple(sorted(planned, key=lambda r: (r.old_path, r.stable_id)))
    return RelocatePlan(
        plan_id=_plan_id(str(old_root), str(new_root), ordered),
        from_prefix=str(old_root),
        to_prefix=str(new_root),
        rows=ordered,
    )


def plan_nearby(
    located: LocatedTrack,
    missing_rows: Iterable[RelocateSourceRow],
    *,
    exists: Callable[[str], bool],
) -> RelocatePlan:
    """Propose the located track's folder move for every missing track under its old folder."""
    old_folder = PurePath(located.previous_path).parent
    new_folder = PurePath(located.current_path).parent
    if old_folder == new_folder:
        raise NotAFolderMove(f"{located.stable_id} was found in the folder it was recorded in ({old_folder})")
    return plan_folder_relocate(missing_rows, from_prefix=str(old_folder), to_prefix=str(new_folder), exists=exists)


__all__ = [
    "InvalidRelocatePrefix",
    "LocatedTrack",
    "NotAFolderMove",
    "RelocatePlan",
    "RelocatePlanRow",
    "RelocateSourceRow",
    "plan_folder_relocate",
    "plan_nearby",
]
