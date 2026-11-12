"""Hard-linked snapshot of a USB stick tree with a SHA-256 manifest (issue #12229).

``create_snapshot(source, dest)`` mirrors a user-selected local stick tree
into ``dest`` (which must not exist yet) by hard-linking every regular file,
and writes ``<dest>.manifest.json`` beside it: one entry per regular file with
its POSIX path relative to the tree root, its size and its SHA-256.
``verify_snapshot(dest)`` re-hashes the snapshot against that manifest.

Rules, after "Test doubles and locked real fixtures" in AGENTS.md (immutable
sources, checksums decide):

* The source is only read. Nothing is ever written inside it.
* A regular file is hard-linked, never copied. When the filesystem refuses a
  link (another volume, a filesystem without links) the snapshot fails and its
  partial tree is removed; there is no copy fallback.
* A symlink, FIFO, socket or device file is refused by default, naming every
  such entry. With ``allow_unsupported=True`` each one is listed under
  ``unsupported`` with its kind and is NOT placed in the snapshot, so it can
  never read as a verified regular file. Symlinks are never followed.
* The manifest is deterministic: sorted entries, no timestamps and no absolute
  paths, so the same tree gives the same bytes and the same manifest SHA-256.

CLI: ``python -m apps.sync.usb snapshot`` and ``snapshot-verify``.
Requirement: USBEXP-12729.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "mdt.usb.snapshot-manifest/1"
MANIFEST_SUFFIX = ".manifest.json"
_CHUNK = 1024 * 1024


class SnapshotError(Exception):
    """A refused or failed snapshot. ``code`` is stable; ``details`` lists entries."""

    def __init__(self, code: str, message: str, details: list[dict[str, str]] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or []


@dataclass(frozen=True)
class SnapshotResult:
    dest: Path
    manifest_path: Path
    manifest_sha256: str
    file_count: int
    dir_count: int
    unsupported: list[dict[str, str]]


@dataclass
class VerifyReport:
    matched: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    not_regular: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.mismatched or self.missing or self.not_regular or self.unexpected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "matched": len(self.matched),
            "mismatched": self.mismatched,
            "missing": self.missing,
            "not_regular": self.not_regular,
            "unexpected": self.unexpected,
            "unsupported": self.unsupported,
        }


def manifest_path_for(dest: Path) -> Path:
    """The manifest sits beside the snapshot directory, never inside it."""
    dest = Path(dest)
    return dest.with_name(dest.name + MANIFEST_SUFFIX)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while chunk := fh.read(_CHUNK):
                digest.update(chunk)
    except OSError as exc:
        raise SnapshotError("read_failed", f"cannot read {path}: {exc.strerror}") from exc
    return digest.hexdigest()


def _kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    return "other"


def _scan(root: Path) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """Walk ``root`` without following links: (dirs, files, unsupported), sorted."""
    dirs: list[str] = []
    files: list[str] = []
    unsupported: list[dict[str, str]] = []
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        try:
            with os.scandir(root / rel_dir if rel_dir else root) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise SnapshotError("scan_failed", f"cannot read {rel_dir or '.'} under {root}: {exc.strerror}") from exc
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                kind = _kind(entry.stat(follow_symlinks=False).st_mode)
            except OSError as exc:
                raise SnapshotError("scan_failed", f"cannot stat {entry.name} in {root}: {exc.strerror}") from exc
            if kind == "dir":
                dirs.append(rel)
                stack.append(rel)
            elif kind == "file":
                files.append(rel)
            else:
                unsupported.append({"kind": kind, "path": rel})
    dirs.sort()
    files.sort()
    unsupported.sort(key=lambda e: e["path"])
    return dirs, files, unsupported


def _check_paths(source: Path, dest: Path) -> tuple[Path, Path]:
    if source.is_symlink():
        raise SnapshotError("source_is_symlink", f"source {source} is a symlink")
    if not source.is_dir():
        raise SnapshotError("source_not_directory", f"source {source} is not a directory")
    src = source.resolve()
    dst = dest.parent.resolve() / dest.name
    if os.path.lexists(dst) or os.path.lexists(manifest_path_for(dst)):
        raise SnapshotError("destination_exists", f"destination {dst} or its manifest exists")
    if dst == src or src in dst.parents:
        raise SnapshotError("destination_inside_source", f"destination {dst} is inside {src}")
    if not dst.parent.is_dir():
        raise SnapshotError("destination_parent_missing", f"{dst.parent} is not a directory")
    return src, dst


def _render_manifest(dirs: list[str], files: list[dict[str, Any]], unsupported: list[dict[str, str]]) -> bytes:
    body = {"schema": MANIFEST_SCHEMA, "dirs": dirs, "files": files, "unsupported": unsupported}
    return (json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _hard_link(source: Path, target: Path, rel: str) -> None:
    """Hard-link one file and prove it shares the source inode; never copy."""
    try:
        os.link(source, target, follow_symlinks=False)
    except OSError as exc:
        why = "another filesystem" if exc.errno == errno.EXDEV else exc.strerror
        raise SnapshotError(
            "hard_link_failed",
            f"cannot hard-link {rel} ({why}); a snapshot never copies files",
            [{"kind": "file", "path": rel}],
        ) from exc
    s_stat, t_stat = os.lstat(source), os.lstat(target)
    if (s_stat.st_dev, s_stat.st_ino) != (t_stat.st_dev, t_stat.st_ino):
        raise SnapshotError(
            "hard_link_failed",
            f"{rel} is not a hard link to its source",
            [{"kind": "file", "path": rel}],
        )


def create_snapshot(source: Path, dest: Path, *, allow_unsupported: bool = False) -> SnapshotResult:
    """Hard-link ``source`` into a new ``dest`` and write the manifest beside it."""
    src, dst = _check_paths(Path(source), Path(dest))
    dirs, files, unsupported = _scan(src)
    if unsupported and not allow_unsupported:
        names = ", ".join(f"{e['path']} ({e['kind']})" for e in unsupported)
        raise SnapshotError(
            "unsupported_entries",
            f"source holds entries that are not regular files: {names}",
            unsupported,
        )

    partial = dst.with_name(f".{dst.name}.partial-{os.getpid()}")
    entries: list[dict[str, Any]] = []
    manifest = manifest_path_for(dst)
    tmp = manifest.with_name(f".{manifest.name}.tmp-{os.getpid()}")
    partial.mkdir()  # outside the try: never remove a directory this call did not make
    renamed = False
    try:
        for rel in dirs:
            (partial / rel).mkdir()
        for rel in files:
            target = partial / rel
            _hard_link(src / rel, target, rel)
            size = os.lstat(target).st_size
            entries.append({"path": rel, "sha256": sha256_file(target), "size": size})
        manifest_bytes = _render_manifest(dirs, entries, unsupported)
        tmp.write_bytes(manifest_bytes)
        os.rename(partial, dst)
        renamed = True
        os.replace(tmp, manifest)
    except BaseException as exc:
        # only links we made; sources keep their data. A failed run leaves no dst or manifest.
        shutil.rmtree(dst if renamed else partial, ignore_errors=True)
        tmp.unlink(missing_ok=True)
        if isinstance(exc, OSError):
            raise SnapshotError("write_failed", f"cannot write snapshot {dst}: {exc.strerror}") from exc
        raise
    return SnapshotResult(
        dest=dst,
        manifest_path=manifest,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        file_count=len(entries),
        dir_count=len(dirs),
        unsupported=unsupported,
    )


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise SnapshotError("manifest_unreadable", f"cannot read manifest {path}: {exc}") from exc
    return parse_manifest(raw, path)


def parse_manifest(raw: bytes, path: Path) -> dict[str, Any]:
    """Decode and schema-check manifest bytes already read from ``path``."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise SnapshotError("manifest_unreadable", f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != MANIFEST_SCHEMA:
        raise SnapshotError("manifest_invalid", f"{path} is not a {MANIFEST_SCHEMA} manifest")
    files = data.get("files")
    if not isinstance(files, list) or not all(
        isinstance(e, dict) and isinstance(e.get("path"), str) and isinstance(e.get("sha256"), str) for e in files
    ):
        raise SnapshotError("manifest_invalid", f"{path} has a file entry without path or sha256")
    return data


def verify_snapshot(dest: Path, manifest: Path | None = None) -> VerifyReport:
    """Re-hash ``dest`` against its manifest; any difference fails the report."""
    dest = Path(dest)
    data = load_manifest(manifest or manifest_path_for(dest))
    if not dest.is_dir() or dest.is_symlink():
        raise SnapshotError("snapshot_missing", f"snapshot {dest} is not a directory")
    report = VerifyReport()
    report.unsupported = sorted(e["path"] for e in data.get("unsupported", []) if isinstance(e, dict) and "path" in e)
    expected = {e["path"]: e for e in data["files"]}
    for rel in sorted(expected):
        path = dest / rel
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            report.missing.append(rel)
            continue
        if not stat.S_ISREG(mode):
            report.not_regular.append(rel)
        elif sha256_file(path) == expected[rel]["sha256"]:
            report.matched.append(rel)
        else:
            report.mismatched.append(rel)
    _, present, odd = _scan(dest)
    extra = [rel for rel in present if rel not in expected]
    extra += [e["path"] for e in odd if e["path"] not in expected]
    report.unexpected = sorted(extra)
    return report
