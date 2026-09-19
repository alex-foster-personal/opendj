"""Copy + transcode primitives for USB apply.

Each executor:

1. Ensures ``op.dst.parent`` exists.
2. Writes to ``<dst>.part`` first.
3. Calls :func:`os.fsync` on the file and (on POSIX) the parent dir.
4. Atomic ``os.rename`` to the final ``dst``.
5. Re-hashes the destination to verify bytes arrived intact (for copy)
   or to compute the new ``content_hash`` (for transcode, since the
   bytes legitimately differ from source).

On failure we clean the ``.part`` file so callers never see a partial
result sitting on the drive.
"""
from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from apps.shared.hashing import sha256_file
from apps.sync.usb.diff import Op
from apps.sync.usb.profile import Profile


class CopyError(RuntimeError):
    """Raised when a copy/transcode op fails post-write verification."""


@dataclass(slots=True)
class CopyResult:
    op: Op
    ok: bool
    actual_hash: str
    dst_size: int
    error: str | None = None
    backup_path: Path | None = None


def _fsync_path(path: Path) -> None:
    """fsync file + parent directory (POSIX only for the directory)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    # Directory fsync is a no-op on macOS (HFS+/APFS) but harmless.
    parent = path.parent
    try:
        dfd = os.open(parent, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(dfd)
        except OSError:
            pass
    finally:
        os.close(dfd)


def _backup_existing(dst: Path) -> Path:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = dst.with_name(dst.name + f".bak-{ts}")
    dst.rename(bak)
    return bak


def _prepare_dst(op: Op, profile: Profile) -> Path | None:
    """Ensure dst dir exists; handle conflict_policy. Returns backup path or None."""
    op.dst.parent.mkdir(parents=True, exist_ok=True)
    if op.dst.exists() and profile.conflict_policy == "backup-then-overwrite":
        return _backup_existing(op.dst)
    return None


def copy_one(op: Op, profile: Profile) -> CopyResult:
    """Copy ``op.src -> op.dst`` atomically."""
    if op.src is None:
        return CopyResult(op=op, ok=False, actual_hash="", dst_size=0, error="src is None")

    backup = _prepare_dst(op, profile)
    part = op.dst.with_name(op.dst.name + ".part")
    try:
        # Ensure any leftover .part from a previous crash is gone.
        if part.exists():
            part.unlink()
        shutil.copy2(op.src, part)
        _fsync_path(part)
        os.replace(part, op.dst)
        _fsync_path(op.dst)

        actual = sha256_file(op.dst)
        if op.expected_hash and actual != op.expected_hash:
            return CopyResult(
                op=op,
                ok=False,
                actual_hash=actual,
                dst_size=op.dst.stat().st_size,
                error=(
                    f"post-write hash mismatch: "
                    f"expected {op.expected_hash}, got {actual}"
                ),
                backup_path=backup,
            )
        return CopyResult(
            op=op,
            ok=True,
            actual_hash=actual,
            dst_size=op.dst.stat().st_size,
            backup_path=backup,
        )
    except OSError as exc:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        return CopyResult(
            op=op,
            ok=False,
            actual_hash="",
            dst_size=0,
            error=f"{type(exc).__name__}: {exc}",
            backup_path=backup,
        )


def transcode_one(
    op: Op,
    profile: Profile,
    *,
    ffmpeg: str = "ffmpeg",
) -> CopyResult:
    """Transcode ``op.src`` to ``op.dst`` as MP3@320 via ffmpeg."""
    if op.src is None:
        return CopyResult(op=op, ok=False, actual_hash="", dst_size=0, error="src is None")

    backup = _prepare_dst(op, profile)
    part = op.dst.with_name(op.dst.name + ".part")
    try:
        if part.exists():
            part.unlink()
        args = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(op.src),
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "320k",
            "-map_metadata",
            "0",
            "-id3v2_version",
            "3",
            str(part),
        ]
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            if part.exists():
                part.unlink()
            return CopyResult(
                op=op,
                ok=False,
                actual_hash="",
                dst_size=0,
                error=f"ffmpeg exit {proc.returncode}: {proc.stderr.strip()[:200]}",
                backup_path=backup,
            )
        _fsync_path(part)
        os.replace(part, op.dst)
        _fsync_path(op.dst)
        actual = sha256_file(op.dst)
        return CopyResult(
            op=op,
            ok=True,
            actual_hash=actual,  # transcode hash differs from source by design
            dst_size=op.dst.stat().st_size,
            backup_path=backup,
        )
    except OSError as exc:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        return CopyResult(
            op=op,
            ok=False,
            actual_hash="",
            dst_size=0,
            error=f"{type(exc).__name__}: {exc}",
            backup_path=backup,
        )


def delete_one(op: Op) -> CopyResult:
    """Delete ``op.dst`` (typically an orphan file not in the profile)."""
    try:
        if op.dst.exists():
            op.dst.unlink()
        return CopyResult(op=op, ok=True, actual_hash="", dst_size=0)
    except OSError as exc:
        return CopyResult(
            op=op,
            ok=False,
            actual_hash="",
            dst_size=0,
            error=f"{type(exc).__name__}: {exc}",
        )


def rename_one(op: Op, *, from_path: Path) -> CopyResult:
    """Rename ``from_path -> op.dst`` in-place (remediate-drift).

    ``backup_path`` is set to ``from_path`` so the reversal script can emit
    the inverse ``mv`` that restores the original location.
    """
    try:
        op.dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(from_path, op.dst)
        _fsync_path(op.dst)
        return CopyResult(
            op=op,
            ok=True,
            actual_hash=op.expected_hash or "",
            dst_size=op.dst.stat().st_size,
            backup_path=from_path,
        )
    except OSError as exc:
        return CopyResult(
            op=op,
            ok=False,
            actual_hash="",
            dst_size=0,
            error=f"{type(exc).__name__}: {exc}",
        )


__all__ = [
    "CopyError",
    "CopyResult",
    "copy_one",
    "transcode_one",
    "delete_one",
    "rename_one",
]
