"""Negative-control carrier restore for installed-app acceptance (#6070).

Split out of ``scripts/app_acceptance.py`` to keep that file under the
file-size ratchet. After analysis has observed the truncated negative-control
carrier, the original verified bytes are written back so later checks (stems)
measure the real track. Only paths inside the run's isolated HOME are touched.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.app_acceptance import Run


def _write_fd_all(fd: int, payload: bytes) -> None:
    """Write every byte of ``payload``; ``os.write`` may return a short count."""
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        written = os.write(fd, view[offset:])
        if written <= 0:
            raise OSError(f"short write at offset {offset} of {len(view)}")
        offset += written


def _negative_carrier_original_bytes(run: Run) -> bytes:
    """Verified full carrier bytes for the negative-control track title."""
    track = next(entry for entry in run.fixture.tracks if entry["title"] == run.fixture.negative_title)
    path = run.fixture.root / track["file"]
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != track["sha256"]:
        raise ValueError(f"carrier source sha256 {digest[:12]} != fixture {track['sha256'][:12]}")
    return data


def _restore_negative_carrier(run: Run, file_path: str) -> str | None:
    """Put the imported carrier back after analysis observed the truncation."""
    title = run.fixture.negative_title
    dest = Path(file_path)
    if dest.is_symlink():
        return f"analysis: negative: {title}: restore target {dest} is a symlink"
    try:
        resolved = dest.resolve(strict=True)
    except OSError as exc:
        return f"analysis: negative: {title}: could not resolve restore target {dest}: {exc}"
    home = run.home.resolve()
    if home != resolved and home not in resolved.parents:
        return f"analysis: negative: {title}: restore target {dest} resolves outside the isolated HOME ({resolved})"
    try:
        payload = _negative_carrier_original_bytes(run)
    except ValueError as exc:
        return f"analysis: negative: {title}: {exc}"
    flags = os.O_WRONLY | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(resolved, flags)
    except OSError as exc:
        return f"analysis: negative: {title}: could not restore {dest.name}: {exc}"
    try:
        _write_fd_all(fd, payload)
    except OSError as exc:
        os.close(fd)
        return f"analysis: negative: {title}: could not restore {dest.name}: {exc}"
    os.close(fd)
    try:
        on_disk = resolved.read_bytes()
    except OSError as exc:
        return f"analysis: negative: {title}: could not verify {dest.name}: {exc}"
    if on_disk != payload:
        got = hashlib.sha256(on_disk).hexdigest()
        want = hashlib.sha256(payload).hexdigest()
        return f"analysis: negative: {title}: restore sha256 {got[:12]} != verified {want[:12]}"
    return None


__all__ = ["_negative_carrier_original_bytes", "_restore_negative_carrier", "_write_fd_all"]
