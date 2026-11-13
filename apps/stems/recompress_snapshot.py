"""Bundle fingerprint, recorded source and decode marks for the stem recompress pass (split from apps/stems/recompress.py)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from apps.shared.hashing import sha256_file_hex
from apps.shared.shared_read_open import open_read_shared
from apps.stems.recompress_classify import (
    BundleChangedError,
)


def bundle_fingerprint(bundle_dir: Path) -> str:
    """SHA-256 over every file's relative path and bytes; ``"absent"`` if gone.

    Content, not mtimes: a coarse filesystem clock (1-2 s on HFS+ or FAT) can
    give a same-size rewrite the same mtime. Each part is read through
    :func:`open_read_shared`: the swap hashes outside the bundle lock, and
    on Windows a plain open would make a publisher's rename of this bundle
    fail with a sharing violation (Codex P1 on #6820). The seam refuses a
    symlinked part, whose ``OSError`` stops this bundle.
    """
    if not bundle_dir.is_dir():
        return "absent"
    digest = hashlib.sha256()
    for path in sorted(p for p in bundle_dir.rglob("*") if p.is_file()):
        digest.update(path.relative_to(bundle_dir).as_posix().encode("utf-8") + b"\0")
        with os.fdopen(open_read_shared(path), "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


#: What a manifest written before sources were hashed carries in ``source.sha256``.
_UNRECORDED_SHA256 = "0" * 64


def _recorded_source_sha256(raw: dict[str, Any]) -> str | None:
    """The source hash the separation recorded, or None when it recorded none."""
    source = raw.get("source")
    value = source.get("sha256") if isinstance(source, dict) else None
    if not isinstance(value, str) or value.lower() == _UNRECORDED_SHA256:
        return None
    return value.lower()


def _require_recorded_source(stable_id: str, raw: dict[str, Any], source_path: Path) -> str:
    """Hash the source and refuse it when the stems recorded another one.

    A file overwritten in place with another recording of about the same
    length passes the path and duration checks, so where the separation
    recorded ``source.sha256`` the bytes on disk must still be those (Codex
    on #6820). A manifest from before sources were hashed records none, and
    is judged by path and duration alone. Returns the hash, which the swap
    checks again so a source rewritten during the encode is caught too.
    """
    digest = sha256_file_hex(source_path)
    recorded = _recorded_source_sha256(raw)
    if recorded is not None and recorded != digest:
        raise BundleChangedError(
            f"{stable_id}: {source_path.name} is not the source these stems were separated from "
            f"(sha256 {digest[:12]} vs recorded {recorded[:12]}); left as it is"
        )
    return digest


def _decoded_frames(path: Path) -> int:
    """Frames the file really decodes to. Not ``sf.info().frames``: a low-rate
    CBR part (STEM-8231's 32 to 80 kbps) carries no Info header, so libsndfile
    estimates its length from the byte count (measured +3001 frames on 4 s
    at 48 kbps, while the decode is within LAME's two-frame pad)."""
    import soundfile as sf

    with sf.SoundFile(str(path)) as audio:
        return sum(len(block) for block in audio.blocks(blocksize=1 << 16, dtype="float32"))


def _marks(path: Path | None) -> tuple[tuple[object, ...], ...] | None:
    """``path`` and every file under it as (relative name, device, inode, ctime, size, mtime); None when gone.

    Taken under the bundle lock before the content hashes and compared under
    it again before the swap: every bundle writer swaps a whole directory in
    (new inodes) and a write in place moves the change time on POSIX.
    """
    if path is None:
        return ()
    try:
        info = path.stat()
        marks: list[tuple[object, ...]] = [
            ("", info.st_dev, info.st_ino, info.st_ctime_ns, info.st_size, info.st_mtime_ns)
        ]
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                child_info = child.stat()
                marks.append(
                    (
                        child.relative_to(path).as_posix(),
                        child_info.st_dev,
                        child_info.st_ino,
                        child_info.st_ctime_ns,
                        child_info.st_size,
                        child_info.st_mtime_ns,
                    )
                )
    except (FileNotFoundError, NotADirectoryError):
        return None
    return tuple(marks)
