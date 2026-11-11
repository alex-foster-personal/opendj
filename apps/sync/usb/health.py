"""Read-only USB export health for Pioneer deck classes (rules v0).

Table-driven pass/fail per deck class from filesystem, required export
artifacts, and path constraints. Never writes to the stick.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from apps.sync.usb.layout import path_segment_is_deck_safe

RULES_VERSION = "v0"

# Normalized filesystem tokens returned by :func:`detect_filesystem`.
FAT32_ALIASES = frozenset({"fat32", "msdos", "ms-dos", "fat", "vfat"})
EXFAT_ALIASES = frozenset({"exfat"})

PIONEER_EXPORT_PDB = PurePosixPath("PIONEER", "rekordbox", "export.pdb")
PIONEER_ONELIBRARY = PurePosixPath("PIONEER", "rekordbox", "exportLibrary.db")

# Conservative FAT32 path limit used by CDJ-2000-era readers (bytes, UTF-8).
FAT32_MAX_PATH_BYTES = 240

DECK_CLASS_IDS: tuple[str, ...] = (
    "cdj_nxs2",
    "cdj_3000",
    "cdj_3000x",
)


@dataclass(frozen=True, slots=True)
class DeckClassRule:
    deck_class: str
    label: str
    allowed_filesystems: frozenset[str]
    required_rel_paths: tuple[PurePosixPath, ...]
    max_path_bytes: int | None


V0_DECK_RULES: tuple[DeckClassRule, ...] = (
    DeckClassRule(
        deck_class="cdj_nxs2",
        label="CDJ-2000NXS2 class (Device Library, FAT32)",
        allowed_filesystems=frozenset({"fat32", "msdos"}),
        required_rel_paths=(PIONEER_EXPORT_PDB,),
        max_path_bytes=FAT32_MAX_PATH_BYTES,
    ),
    DeckClassRule(
        deck_class="cdj_3000",
        label="CDJ-3000 / XDJ-RX3 (Device Library)",
        allowed_filesystems=frozenset({"fat32", "msdos", "exfat"}),
        required_rel_paths=(PIONEER_EXPORT_PDB,),
        max_path_bytes=None,
    ),
    DeckClassRule(
        deck_class="cdj_3000x",
        label="CDJ-3000X / OPUS-QUAD / XDJ-AZ (Device Library Plus)",
        allowed_filesystems=frozenset({"fat32", "msdos", "exfat"}),
        required_rel_paths=(PIONEER_ONELIBRARY,),
        max_path_bytes=None,
    ),
)


@dataclass(frozen=True, slots=True)
class MountScan:
    mount_root: Path
    filesystem: str
    has_export_pdb: bool
    has_onelibrary: bool
    path_violations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeckClassVerdict:
    deck_class: str
    label: str
    ok: bool
    failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VolumeExportHealth:
    volume_id: str
    rules_version: str
    filesystem: str
    scanned_paths: int
    deck_classes: tuple[DeckClassVerdict, ...]


def normalize_filesystem_token(raw: str) -> str:
    text = raw.strip().lower().replace("-", "").replace(" ", "")
    if text in FAT32_ALIASES or "fat32" in text or text == "fat":
        return "fat32"
    if text in EXFAT_ALIASES or text == "exfat":
        return "exfat"
    if text in {"msdos", "msdosfat"}:
        return "msdos"
    return text or "unknown"


def detect_filesystem(
    mount_root: Path,
    *,
    platform: str | None = None,
    diskutil_command: str | None = None,
    run_diskutil: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> str:
    """Return a normalized filesystem token for ``mount_root``, or ``unknown``."""
    current = sys.platform if platform is None else platform
    if current == "darwin":
        command = diskutil_command
        if command is None:
            from apps.shared.macos_diskutil import resolve_diskutil

            command = resolve_diskutil()
        if command is None:
            return "unknown"
        runner = run_diskutil
        if runner is None:
            runner = lambda args: subprocess.run(
                [command, *args],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        try:
            completed = runner(["info", "-plist", os.fspath(mount_root)])
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return "unknown"
        text = completed.stdout
        for key in ("FilesystemPersonality", "ContentType", "FileSystemPersonality"):
            match = re.search(rf"<key>{key}</key>\s*<string>([^<]+)</string>", text)
            if match:
                return normalize_filesystem_token(match.group(1))
        for line in text.splitlines():
            if "File System Personality:" in line or "Content (IOContent)" in line:
                _, _, tail = line.partition(":")
                if tail.strip():
                    return normalize_filesystem_token(tail)
    # Linux / Windows CI: best-effort from /proc/mounts when present.
    proc = Path("/proc/mounts")
    if proc.is_file():
        target = os.fspath(mount_root.resolve())
        for line in proc.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] == target:
                return normalize_filesystem_token(parts[2])
    return "unknown"


def _rel_from_root(root: Path, path: Path) -> PurePosixPath | None:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    return PurePosixPath(*rel.parts)


def _path_byte_length(parts: tuple[str, ...]) -> int:
    return len("/".join(parts).encode("utf-8"))


def _segment_has_forbidden_chars(name: str) -> bool:
    return not path_segment_is_deck_safe(name)


def scan_mount_paths(
    mount_root: Path,
    *,
    max_entries: int = 5000,
) -> tuple[int, tuple[str, ...]]:
    """Walk ``mount_root`` read-only; return (file_count, violation messages)."""
    if not mount_root.is_dir():
        return 0, ("mount: not a directory",)
    violations: list[str] = []
    seen = 0
    root = mount_root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)
        rel_dir = _rel_from_root(root, current)
        if rel_dir is None:
            dirnames.clear()
            continue
        for name in dirnames:
            if _segment_has_forbidden_chars(name):
                violations.append(f"path:{rel_dir / name}: forbidden characters")
        for name in filenames:
            seen += 1
            if seen > max_entries:
                violations.append("scan: stopped at entry limit")
                return seen, tuple(violations)
            rel = rel_dir / name
            if _segment_has_forbidden_chars(name):
                violations.append(f"path:{rel}: forbidden characters")
            if FAT32_MAX_PATH_BYTES is not None:
                if _path_byte_length(rel.parts) > FAT32_MAX_PATH_BYTES:
                    violations.append(f"path:{rel}: exceeds {FAT32_MAX_PATH_BYTES} bytes")
        if violations and len(violations) >= 20:
            break
    return seen, tuple(violations)


def scan_mount(
    mount_root: Path,
    *,
    filesystem: str | None = None,
    diskutil_command: str | None = None,
    run_diskutil: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> MountScan:
    root = mount_root.resolve()
    fs = (
        filesystem
        if filesystem is not None
        else detect_filesystem(
            root,
            diskutil_command=diskutil_command,
            run_diskutil=run_diskutil,
        )
    )
    has_pdb = (root / PIONEER_EXPORT_PDB).is_file()
    has_one = (root / PIONEER_ONELIBRARY).is_file()
    _, violations = scan_mount_paths(root)
    return MountScan(
        mount_root=root,
        filesystem=fs,
        has_export_pdb=has_pdb,
        has_onelibrary=has_one,
        path_violations=violations,
    )


def _path_requirement_met(root: Path, rel: PurePosixPath) -> bool:
    return (root / Path(*rel.parts)).is_file()


def evaluate_deck_class(rule: DeckClassRule, scan: MountScan) -> DeckClassVerdict:
    failures: list[str] = []
    if scan.filesystem not in rule.allowed_filesystems:
        failures.append(f"format:{scan.filesystem}")
    for rel in rule.required_rel_paths:
        if not _path_requirement_met(scan.mount_root, rel):
            failures.append(f"path:{rel.as_posix()}")
    if rule.max_path_bytes is not None:
        long_paths = [item for item in scan.path_violations if item.startswith("path:") and "exceeds" in item]
        failures.extend(long_paths)
    char_paths = [item for item in scan.path_violations if item.startswith("path:") and "forbidden" in item]
    failures.extend(char_paths)
    return DeckClassVerdict(
        deck_class=rule.deck_class,
        label=rule.label,
        ok=not failures,
        failures=tuple(failures),
    )


def evaluate_volume_health(
    *,
    volume_id: str,
    mount_root: Path,
    filesystem: str | None = None,
    diskutil_command: str | None = None,
    run_diskutil: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> VolumeExportHealth:
    scan = scan_mount(
        mount_root,
        filesystem=filesystem,
        diskutil_command=diskutil_command,
        run_diskutil=run_diskutil,
    )
    count, _ = scan_mount_paths(scan.mount_root)
    classes = tuple(evaluate_deck_class(rule, scan) for rule in V0_DECK_RULES)
    return VolumeExportHealth(
        volume_id=volume_id,
        rules_version=RULES_VERSION,
        filesystem=scan.filesystem,
        scanned_paths=count,
        deck_classes=classes,
    )


__all__ = [
    "DECK_CLASS_IDS",
    "DeckClassVerdict",
    "MountScan",
    "RULES_VERSION",
    "V0_DECK_RULES",
    "VolumeExportHealth",
    "detect_filesystem",
    "evaluate_deck_class",
    "evaluate_volume_health",
    "normalize_filesystem_token",
    "scan_mount",
    "scan_mount_paths",
]
