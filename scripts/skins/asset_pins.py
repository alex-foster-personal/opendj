"""Check skin package assets against sha256 pins and per-extension size caps."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

DEFAULT_CAPS: dict[str, int] = {"svg": 256 * 1024, "png": 1024 * 1024, "woff2": 512 * 1024}


def _escapes(rel: str) -> bool:
    p = PurePosixPath(rel)
    if not p.parts or p.is_absolute() or rel.startswith("\\") or ":" in p.parts[0]:
        return True
    return ".." in p.parts


def check_asset_pins(package_dir: Path, files: Mapping[str, str], caps: Mapping[str, int] | None = None) -> list[str]:
    """Return a list of error strings; empty means every asset is pinned and within caps."""
    caps = DEFAULT_CAPS if caps is None else caps
    root = Path(package_dir).resolve()
    errors: list[str] = []
    listed: set[str] = set()
    for rel, pin in files.items():
        if _escapes(rel):
            errors.append(f"path escapes package: {rel}")
            continue
        path = (root / rel).resolve()
        if not path.is_relative_to(root):
            errors.append(f"path escapes package: {rel}")
            continue
        listed.add(PurePosixPath(rel).as_posix())
        if not path.is_file():
            errors.append(f"missing listed file: {rel}")
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != pin.lower():
            errors.append(f"sha256 mismatch: {rel}")
        ext = path.suffix.lstrip(".").lower()
        cap = caps.get(ext)
        if cap is not None and len(data) > cap:
            errors.append(f"over size cap: {rel} is {len(data)} bytes, cap {cap}")
    skin_dir = root / "skin"
    if skin_dir.is_dir():
        for f in sorted(skin_dir.rglob("*")):
            if f.is_file():
                rel = f.relative_to(root).as_posix()
                if rel not in listed:
                    errors.append(f"unpinned file: {rel}")
    return errors
