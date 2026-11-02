"""Load checksum-pinned JSON capture fixtures shared by the genre test support modules."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def load_captured(fixtures_dir: Path, name: str, *, manifest_version: int | None = None) -> dict[str, Any]:
    """A captured reply under ``fixtures_dir``, verified against its manifest before parsing."""
    manifest = json.loads((fixtures_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest_version is not None:
        assert manifest["version"] == manifest_version, manifest["version"]
    entry = manifest["files"][f"{name}.json"]
    raw = (fixtures_dir / f"{name}.json").read_bytes()
    assert len(raw) == entry["bytes"], f"{name}.json is not the captured fixture (size)"
    assert hashlib.sha256(raw).hexdigest() == entry["sha256"], f"{name}.json is not the captured fixture (sha256)"
    return json.loads(raw.decode("utf-8"))


__all__ = ["load_captured"]
