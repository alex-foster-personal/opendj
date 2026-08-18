"""Stable-ID ledger and destination audit for a remote library crate.

The owner creates the expected ledger from its live databases and files.  The
replica independently stats every declared destination and must reproduce the
same digest.  No SQLite database is copied between machines.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from apps.shared import fs_residency, library_mode


class CrateIndexError(RuntimeError):
    """The crate ledger is malformed or disagrees with replica storage."""


@dataclass(frozen=True)
class CrateAudit:
    ok: bool
    expected_count: int
    actual_count: int
    expected_bytes: int
    actual_bytes: int
    expected_digest: str
    actual_digest: str
    missing: tuple[str, ...]
    size_mismatches: tuple[str, ...]
    mtime_mismatches: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "expected_count": self.expected_count,
            "actual_count": self.actual_count,
            "expected_bytes": self.expected_bytes,
            "actual_bytes": self.actual_bytes,
            "expected_digest": self.expected_digest,
            "actual_digest": self.actual_digest,
            "missing": list(self.missing),
            "size_mismatches": list(self.size_mismatches),
            "mtime_mismatches": list(self.mtime_mismatches),
        }


def _contained(crate_root: Path, raw: str) -> Path:
    root = crate_root.resolve()
    candidate = Path(raw).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise CrateIndexError(f"crate destination escapes {root}: {candidate}")
    return candidate


def _entry_line(entry: Mapping[str, Any], *, actual: Path | None = None) -> str:
    path = str(entry["dest"])
    size = int(entry["bytes"])
    mtime_s = int(entry.get("mtime_s", 0))
    if actual is not None:
        stat = actual.stat()
        size = stat.st_size
        mtime_s = int(stat.st_mtime)
    stable_ids = ",".join(sorted(str(item) for item in entry.get("stable_ids", [])))
    return f"{entry['kind']}\0{path}\0{size}\0{mtime_s}\0{stable_ids}\n"


def ledger_digest(
    entries: list[Mapping[str, Any]], *, actual: Mapping[str, Path] | None = None
) -> str:
    """Digest the same logical entries on owner and replica."""

    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: str(item["dest"])):
        path = None if actual is None else actual.get(str(entry["dest"]))
        digest.update(_entry_line(entry, actual=path).encode("utf-8"))
    return digest.hexdigest()


def audit_manifest(manifest: Mapping[str, Any], *, crate_root: Path) -> CrateAudit:
    """Rebuild the replica book from real files and compare it to the owner book."""

    raw_entries = manifest.get("files")
    if not isinstance(raw_entries, list):
        raise CrateIndexError("crate manifest files must be a list")
    entries: list[Mapping[str, Any]] = []
    actual: dict[str, Path] = {}
    missing: list[str] = []
    size_mismatches: list[str] = []
    mtime_mismatches: list[str] = []
    actual_bytes = 0
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise CrateIndexError("crate manifest file entry must be an object")
        for key in ("kind", "source", "dest", "bytes"):
            if key not in raw:
                raise CrateIndexError(f"crate manifest file entry lacks {key}")
        entry: Mapping[str, Any] = raw
        entries.append(entry)
        destination = _contained(crate_root, str(entry["dest"]))
        if not fs_residency.is_materialised(destination):
            missing.append(str(destination))
            continue
        stat = destination.stat()
        actual[str(entry["dest"])] = destination
        actual_bytes += stat.st_size
        if stat.st_size != int(entry["bytes"]):
            size_mismatches.append(str(destination))
        expected_mtime = int(entry.get("mtime_s", 0))
        if expected_mtime and int(stat.st_mtime) != expected_mtime:
            mtime_mismatches.append(str(destination))

    expected_digest = str(manifest.get("ledger_digest") or ledger_digest(entries))
    actual_digest = ledger_digest(entries, actual=actual) if not missing else ""
    expected_bytes = sum(int(entry["bytes"]) for entry in entries)
    ok = not (missing or size_mismatches or mtime_mismatches) and (
        actual_digest == expected_digest
    )
    return CrateAudit(
        ok=ok,
        expected_count=len(entries),
        actual_count=len(actual),
        expected_bytes=expected_bytes,
        actual_bytes=actual_bytes,
        expected_digest=expected_digest,
        actual_digest=actual_digest,
        missing=tuple(missing),
        size_mismatches=tuple(size_mismatches),
        mtime_mismatches=tuple(mtime_mismatches),
    )


@lru_cache(maxsize=8)
def _read_track_files(manifest_path: str, mtime_ns: int, size: int) -> dict[str, str]:
    del mtime_ns, size
    raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    track_files = raw.get("track_files")
    if not isinstance(track_files, dict):
        return {}
    return {
        str(stable_id): str(path)
        for stable_id, path in track_files.items()
        if isinstance(path, str)
    }


def resolve_crate_audio(
    stable_id: str,
    *,
    environ: Mapping[str, str] | None = None,
    manifest_path: Path | None = None,
) -> Path | None:
    """Return a materialised indexed audio file in explicit remote mode only."""

    if library_mode.library_mode(environ=environ) != "remote":
        return None
    root = library_mode.crate_root(environ=environ)
    path = manifest_path or root / "manifest.json"
    if not path.is_file():
        return None
    stat = path.stat()
    indexed = _read_track_files(str(path), stat.st_mtime_ns, stat.st_size).get(
        stable_id
    )
    if indexed is None:
        return None
    candidate = _contained(root, indexed)
    return candidate if fs_residency.is_materialised(candidate) else None


__all__ = [
    "CrateAudit",
    "CrateIndexError",
    "audit_manifest",
    "ledger_digest",
    "resolve_crate_audio",
]
