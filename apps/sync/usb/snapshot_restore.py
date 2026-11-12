"""Restore a USB stick snapshot into a selected root, then prove it byte for byte.

Issue #12230. A snapshot is a directory plus ``<snapshot>.manifest.json``
beside it, as written by ``apps.sync.usb.snapshot`` (issue #12229, schema
``mdt.usb.snapshot-manifest/1``: sorted ``dirs``; ``files`` entries with a
relative POSIX ``path``, ``size`` and ``sha256``; and ``unsupported`` entries
that were never part of the tree). The manifest is read and schema-checked by
that module's ``parse_manifest``; this module adds the stricter checks restore
needs before it writes anything. Restore works only on paths the user names
and never looks for mounted devices.

The order is fixed and every step fails closed:

1. Validate the manifest itself: schema (``snapshot.parse_manifest``), entry types, safe relative paths that
   cannot leave the restore root, and no two entries that collide when case is
   ignored (FAT and exFAT sticks fold case).
2. Validate the snapshot against the manifest: the same dirs, the same regular
   files with the same size and SHA-256, nothing extra, no symlink or special
   file. Any difference refuses the restore before a single destination file
   is written.
3. Write into the restore root, which must be missing or an empty real
   directory. Existing content is never overwritten or merged: every directory
   and file is created exclusively, without following links.
4. Compare the restored tree with the snapshot recursively, byte for byte. The
   receipt always carries that result.

Any failure in steps 3 or 4 removes exactly what this run created and nothing
else: on POSIX every create and removal reaches its parent from the open root
handle without following links, checking each directory on the way is the one
this run made, and rollback removes an entry only while it is still the same
inode; anything replaced is left alone and listed as ``cleanup_incomplete``.

CLI: ``python -m apps.sync.usb restore SNAPSHOT DEST`` (see ``__main__``).
Requirement: USBEXP-12737 (``(private path)``).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from apps.sync.usb.snapshot import SnapshotError, manifest_path_for, parse_manifest

_CHUNK = 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
# Pinned directory handles (POSIX): every create and every rollback removal goes
# through an open handle on a directory this run made, so a parent swapped for a
# link after it was checked can never redirect a write or a removal elsewhere.
_PINNED = bool(_NOFOLLOW and _DIRECTORY) and all(
    f in os.supports_dir_fd for f in (os.open, os.mkdir, os.rmdir, os.unlink, os.stat)
)


class RestoreError(Exception):
    """A refused or failed restore. ``code`` is stable; ``entries`` names paths."""

    def __init__(self, code: str, message: str, entries: list[dict[str, str]] | None = None, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.entries = entries or []
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "entries": self.entries, **self.extra}


@dataclass
class ByteCompare:
    """Recursive byte comparison of a restored tree against its snapshot."""

    files_compared: int = 0
    bytes_compared: int = 0
    mismatched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.mismatched or self.missing or self.unexpected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "method": "recursive byte comparison",
            "files_compared": self.files_compared,
            "bytes_compared": self.bytes_compared,
            "mismatched": self.mismatched,
            "missing": self.missing,
            "unexpected": self.unexpected,
        }


@dataclass(frozen=True)
class RestoreReceipt:
    snapshot: Path
    destination: Path
    manifest: Path
    manifest_sha256: str
    files: int
    dirs: int
    bytes: int
    not_restored: list[dict[str, str]]
    byte_compare: ByteCompare

    @property
    def ok(self) -> bool:
        return self.byte_compare.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "snapshot": str(self.snapshot),
            "destination": str(self.destination),
            "manifest": str(self.manifest),
            "manifest_sha256": self.manifest_sha256,
            "files": self.files,
            "dirs": self.dirs,
            "bytes": self.bytes,
            "not_restored": self.not_restored,
            "byte_compare": self.byte_compare.to_dict(),
        }


def _entry(path: str, problem: str) -> dict[str, str]:
    return {"path": path, "problem": problem}


def _unsafe_reason(rel: str) -> str | None:
    """Why ``rel`` is not a plain relative path inside the root, or None."""
    if not rel or "\x00" in rel or "\\" in rel:
        return "empty, NUL or backslash in path"
    if rel.startswith("/"):
        return "absolute path"
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "empty, '.' or '..' segment"
    if any(":" in part for part in parts):
        return "drive letter or stream marker"
    if PurePosixPath(rel).as_posix() != rel:
        return "not a canonical POSIX path"
    return None


def _load_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    """Read the bytes once (the receipt hashes them), then parse them as #12229 does."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise RestoreError("manifest_unreadable", f"cannot read manifest {path}: {exc}") from exc
    try:
        data = parse_manifest(raw, path)
    except SnapshotError as exc:
        raise RestoreError(exc.code, exc.message, exc.details) from exc
    dirs, unsupported = data.get("dirs"), data.get("unsupported", [])
    if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
        raise RestoreError("manifest_invalid", f"{path}: dirs must be a list of paths")
    if not isinstance(unsupported, list):
        raise RestoreError("manifest_invalid", f"{path}: unsupported must be a list")
    return data, raw


def _good_file_entry(e: object) -> bool:
    return (
        isinstance(e, dict)
        and isinstance(e.get("path"), str)
        and isinstance(e.get("sha256"), str)
        and _DIGEST.fullmatch(e["sha256"]) is not None
        and type(e.get("size")) is int
        and e["size"] >= 0
    )


def _fold(path: str) -> str:
    """Collision key: NFC first, then case-folded, as APFS/HFS+ and FAT/exFAT compare names."""
    return unicodedata.normalize("NFC", path).casefold()


def _collisions(paths: list[str], dirs: list[str]) -> list[dict[str, str]]:
    """Case- or normalization-folded duplicates and entries without a listed parent."""
    seen: dict[str, str] = {}
    found: list[dict[str, str]] = []
    for p in paths:
        key = _fold(p)
        if key in seen:
            found.append(_entry(p, f"collides with {seen[key]} when case and Unicode form are ignored"))
        else:
            seen[key] = p
    dir_keys = {_fold(d) for d in dirs}
    for p in paths:
        for parent in PurePosixPath(p).parents:
            name = parent.as_posix()
            if name != "." and _fold(name) not in dir_keys:
                found.append(_entry(p, f"parent {name} is not a listed dir"))
    return found


def _validate_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    """Load the manifest and refuse anything restore could not write safely."""
    data, raw = _load_manifest(path)
    bad = [
        _entry(str(e.get("path") if isinstance(e, dict) else e), "needs path, size and sha256")
        for e in data["files"]
        if not _good_file_entry(e)
    ]
    if bad:
        raise RestoreError("manifest_invalid", f"{path} has malformed file entries", bad)
    paths = [*data["dirs"], *(e["path"] for e in data["files"])]
    unsafe = [_entry(p, why) for p in paths if (why := _unsafe_reason(p))]
    if unsafe:
        raise RestoreError("unsafe_path", "manifest paths would leave the restore root", unsafe)
    collisions = _collisions(paths, data["dirs"])
    if collisions:
        raise RestoreError("path_collision", "manifest paths collide or are inconsistent", collisions)
    return data, raw


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "symlink" if stat.S_ISLNK(mode) else "special"


def _walk(root: Path) -> tuple[set[str], set[str], dict[str, str]]:
    """(dirs, files, other kinds) under ``root``, never following links."""
    dirs: set[str] = set()
    files: set[str] = set()
    other: dict[str, str] = {}
    pending = [""]
    while pending:
        rel_dir = pending.pop()
        with os.scandir(root / rel_dir if rel_dir else root) as it:
            for entry in it:
                rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                kind = _kind(entry.stat(follow_symlinks=False).st_mode)
                if kind == "dir":
                    dirs.add(rel)
                    pending.append(rel)
                elif kind == "file":
                    files.add(rel)
                else:
                    other[rel] = kind
    return dirs, files, other


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _BINARY)
    with os.fdopen(fd, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _check_snapshot(snapshot: Path, data: dict[str, Any]) -> None:
    """Refuse unless the snapshot tree is exactly what the manifest describes."""
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise RestoreError("snapshot_missing", f"snapshot {snapshot} is not a real directory")
    expected = {e["path"]: e for e in data["files"]}
    problems: list[dict[str, str]] = []
    try:
        dirs, files, other = _walk(snapshot)
        problems += [_entry(p, "missing dir") for p in sorted(set(data["dirs"]) - dirs)]
        problems += [_entry(p, "dir not in manifest") for p in sorted(dirs - set(data["dirs"]))]
        problems += [_entry(p, "file not in manifest") for p in sorted(files - set(expected))]
        problems += [_entry(p, f"{kind} is not a regular file") for p, kind in sorted(other.items())]
        for rel in sorted(expected):
            if rel in other:
                continue
            if rel not in files:
                problems.append(_entry(rel, "missing file"))
            elif os.lstat(snapshot / rel).st_size != expected[rel]["size"]:
                problems.append(_entry(rel, "size differs from manifest"))
            elif _sha256(snapshot / rel) != expected[rel]["sha256"]:
                problems.append(_entry(rel, "sha256 differs from manifest"))
    except OSError as exc:
        raise RestoreError("snapshot_unreadable", f"cannot read snapshot {snapshot}: {exc}") from exc
    if problems:
        raise RestoreError("manifest_mismatch", "snapshot content does not match its manifest", problems)


def _check_destination(dest: Path, snapshot: Path) -> tuple[Path, bool]:
    """Resolve the restore root; it must be missing or an empty real directory."""
    if dest.is_symlink():
        raise RestoreError("destination_is_symlink", f"restore root {dest} is a symlink")
    if not dest.parent.is_dir():
        raise RestoreError("destination_parent_missing", f"{dest.parent} is not a directory")
    root = dest.parent.resolve() / dest.name
    snap = snapshot.resolve()
    if root == snap or snap in root.parents or root in snap.parents:
        raise RestoreError("destination_overlaps_snapshot", f"restore root {root} overlaps snapshot {snap}")
    if not os.path.lexists(root):
        return root, True
    if not root.is_dir():
        raise RestoreError("destination_not_directory", f"restore root {root} is not a directory")
    try:
        with os.scandir(root) as it:
            present = sorted(e.name for e in it)
    except OSError as exc:
        raise RestoreError("destination_unreadable", f"cannot read restore root {root}: {exc}") from exc
    if present:
        raise RestoreError(
            "destination_not_empty",
            f"restore root {root} is not empty; restore never overwrites or merges",
            [_entry(name, "already present") for name in present],
        )
    return root, False


def _inside(root: Path, rel: str) -> Path:
    """``root / rel``, proven not to leave ``root`` even through a swapped-in link."""
    target = root / rel
    real_parent = os.path.realpath(target.parent)
    if os.path.commonpath([str(root), real_parent]) != str(root):
        raise RestoreError("unsafe_path", f"{rel} would be written outside {root}", [_entry(rel, "escapes root")])
    return target


@dataclass(frozen=True)
class _Made:
    """One entry this run created: where, what kind, and which inode it was."""

    rel: str  # "" is the restore root itself
    is_dir: bool
    dev: int
    ino: int


def _split(rel: str) -> tuple[str, str]:
    parent, _, name = rel.rpartition("/")
    return parent, name


class _Tree:
    """Creates and removes entries under the restore root through checked parents.

    With ``_PINNED`` only the restore root stays open. Each create or removal
    reaches its parent by walking down from that handle one component at a time
    with ``O_NOFOLLOW`` and checks every directory on the way is still the inode
    this run made, so a parent swapped for a link (or another directory) can
    never redirect a write or a removal. Open handles are bounded by tree depth,
    not by how many directories a stick has. Elsewhere (Windows) paths are
    re-checked with ``_inside`` before each use.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root_fd: int | None = None
        self.dirs: dict[str, tuple[int, int]] = {}  # rel -> (dev, ino) for every dir on the walk
        self.made: list[_Made] = []

    def close(self) -> None:
        if self.root_fd is not None:
            os.close(self.root_fd)
            self.root_fd = None

    def _record(self, rel: str, is_dir: bool, st: os.stat_result) -> None:
        self.made.append(_Made(rel, is_dir, st.st_dev, st.st_ino))
        if is_dir:
            self.dirs[rel] = (st.st_dev, st.st_ino)

    @staticmethod
    def _open_dir(path: str | Path, parent: int | None = None) -> int:
        return os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=parent)

    @contextlib.contextmanager
    def _parent(self, rel: str) -> Iterator[int]:
        """An fd on ``rel``'s parent, reached from the root without following links."""
        assert self.root_fd is not None
        parent, _ = _split(rel)
        fd = os.dup(self.root_fd)
        try:
            walked = ""
            for part in parent.split("/") if parent else []:
                walked = f"{walked}/{part}" if walked else part
                child = self._open_dir(part, fd)
                os.close(fd)
                fd = child
                st = os.fstat(fd)
                if (st.st_dev, st.st_ino) != self.dirs.get(walked):
                    raise RestoreError(
                        "destination_changed",
                        f"{walked} was replaced while restoring {rel}",
                        [_entry(walked, "replaced")],
                    )
            yield fd
        finally:
            os.close(fd)

    def make_root(self, create: bool) -> None:
        if create:
            self.root.mkdir()
        if _PINNED:
            self.root_fd = self._open_dir(self.root)
            st = os.fstat(self.root_fd)
        else:
            st = os.lstat(self.root)
        if create:
            self._record("", True, st)
        else:
            self.dirs[""] = (st.st_dev, st.st_ino)

    def mkdir(self, rel: str) -> None:
        if not _PINNED:
            target = _inside(self.root, rel)
            target.mkdir()  # raises if it exists, so a dir we did not make is never claimed
            self._record(rel, True, os.lstat(target))
            return
        name = _split(rel)[1]
        with self._parent(rel) as pfd:
            os.mkdir(name, dir_fd=pfd)
            self._record(rel, True, os.stat(name, dir_fd=pfd, follow_symlinks=False))

    def create_file(self, rel: str) -> int:
        """Open a new file exclusively and record it once it is ours."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY
        if _PINNED:
            with self._parent(rel) as pfd:
                fd = os.open(_split(rel)[1], flags, 0o666, dir_fd=pfd)
        else:
            fd = os.open(_inside(self.root, rel), flags, 0o666)
        self._record(rel, False, os.fstat(fd))  # only after O_EXCL succeeded
        return fd

    def rollback(self) -> list[dict[str, str]]:
        """Remove only entries that are still the inodes this run made, newest first.

        Anything replaced, moved or unreachable is left untouched and returned.
        """
        left: list[dict[str, str]] = []
        for made in reversed(self.made):
            try:
                self._remove(made)
            except RestoreError as exc:
                left.append(_entry(made.rel or ".", exc.message))
            except OSError as exc:
                left.append(_entry(made.rel or ".", f"not removed: {exc}"))
        self.close()
        return left

    def _remove(self, made: _Made) -> None:
        if made.rel == "" or not _PINNED:
            self.close()  # the root goes last; its handle is done
            name = _inside(self.root, made.rel) if made.rel else self.root
            self._unlink_if_ours(name, made, {})
            return
        with self._parent(made.rel) as pfd:
            self._unlink_if_ours(_split(made.rel)[1], made, {"dir_fd": pfd})

    @staticmethod
    def _unlink_if_ours(name: str | Path, made: _Made, where: dict[str, Any]) -> None:
        st = os.stat(name, follow_symlinks=False, **where)
        if (st.st_dev, st.st_ino) != (made.dev, made.ino):
            raise RestoreError("cleanup_incomplete", "replaced by another entry; left untouched")
        if made.is_dir:
            os.rmdir(name, **where)
        else:
            os.unlink(name, **where)


def _copy_exclusive(source: Path, tree: _Tree, expected_sha256: str, rel: str) -> int:
    """Copy one file into a path that must not exist yet."""
    digest = hashlib.sha256()
    written = 0
    src_fd = os.open(source, os.O_RDONLY | _NOFOLLOW | _BINARY)
    try:
        dst_fd = tree.create_file(rel)
        try:
            while chunk := os.read(src_fd, _CHUNK):
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    n = os.write(dst_fd, view)
                    view = view[n:]
                    written += n
            os.fsync(dst_fd)
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)
    if digest.hexdigest() != expected_sha256:
        raise RestoreError("snapshot_changed", f"{rel} changed while it was restored", [_entry(rel, "sha256 changed")])
    return written


def _same_bytes(a: Path, b: Path) -> tuple[bool, int]:
    count = 0
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            ca, cb = fa.read(_CHUNK), fb.read(_CHUNK)
            if ca != cb:
                return False, count
            if not ca:
                return True, count
            count += len(ca)


def compare_trees(snapshot: Path, restored: Path) -> ByteCompare:
    """Recursively compare two trees: same dirs, same files, identical bytes."""
    result = ByteCompare()
    s_dirs, s_files, s_other = _walk(Path(snapshot))
    r_dirs, r_files, r_other = _walk(Path(restored))
    result.missing = sorted((s_dirs - r_dirs) | (s_files - r_files) | set(s_other))
    result.unexpected = sorted((r_dirs - s_dirs) | (r_files - s_files) | set(r_other))
    for rel in sorted(s_files & r_files):
        same, count = _same_bytes(Path(snapshot) / rel, Path(restored) / rel)
        result.bytes_compared += count
        result.files_compared += 1
        if not same:
            result.mismatched.append(rel)
    return result


def _write_tree(snapshot: Path, tree: _Tree, data: dict[str, Any], make_root: bool) -> int:
    """Create every dir and file exclusively under the root; ``tree`` records what is ours."""
    tree.make_root(make_root)
    for rel in sorted(data["dirs"], key=lambda d: (d.count("/"), d)):
        tree.mkdir(rel)
    total = 0
    for entry in sorted(data["files"], key=lambda e: e["path"]):
        rel = entry["path"]
        total += _copy_exclusive(snapshot / rel, tree, entry["sha256"], rel)
    return total


def _require_identical(compare: ByteCompare) -> None:
    if not compare.ok:
        raise RestoreError(
            "byte_compare_failed",
            "restored tree differs from the snapshot",
            [_entry(p, "differs") for p in compare.mismatched + compare.missing + compare.unexpected],
            byte_compare=compare.to_dict(),
        )


def restore_snapshot(snapshot: Path, dest: Path, *, manifest: Path | None = None) -> RestoreReceipt:
    """Validate, restore ``snapshot`` into ``dest`` and byte-compare the result."""
    snapshot = Path(snapshot)
    manifest_path = Path(manifest) if manifest else manifest_path_for(snapshot)
    data, raw = _validate_manifest(manifest_path)
    _check_snapshot(snapshot, data)
    root, make_root = _check_destination(Path(dest), snapshot)

    tree = _Tree(root)
    try:
        total = _write_tree(snapshot, tree, data, make_root)
        compare = compare_trees(snapshot, root)
        _require_identical(compare)
    except BaseException as exc:
        left = tree.rollback()
        if isinstance(exc, RestoreError):
            if left:
                exc.extra["cleanup_incomplete"] = left
            raise
        if isinstance(exc, OSError):
            extra = {"cleanup_incomplete": left} if left else {}
            raise RestoreError(
                "write_failed", f"restore into {root} failed and was rolled back: {exc}", **extra
            ) from exc
        raise
    tree.close()
    return RestoreReceipt(
        snapshot=snapshot,
        destination=root,
        manifest=manifest_path,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        files=len(data["files"]),
        dirs=len(data["dirs"]),
        bytes=total,
        not_restored=[e for e in data.get("unsupported", []) if isinstance(e, dict)],
        byte_compare=compare,
    )
