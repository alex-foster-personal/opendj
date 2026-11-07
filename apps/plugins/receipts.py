"""Receipt paths and bounded reads for per-version store install proofs.

A store install keeps the signed catalog index it came from under
``<plugins-dir>/receipts/<id>/<version>.index.json`` plus ``.sig``. These
helpers name those paths and read manifest or receipt bytes with the same
caps verification uses, without loading oversized files whole.
"""

from __future__ import annotations

from pathlib import Path

from apps.plugins.catalog import MAX_INDEX_BYTES
from apps.plugins.manifest import ManifestError
from apps.plugins.regular import open_regular_file

RECEIPTS_DIRNAME: str = "receipts"


def receipt_paths(root: Path, plugin_id: str, version: str) -> tuple[Path, Path]:
    index = root / RECEIPTS_DIRNAME / plugin_id / f"{version}.index.json"
    return index, index.with_name(index.name + ".sig")


def read_capped(path: Path, limit: int) -> bytes | None:
    """At most ``limit`` bytes of ``path``, or None when it holds more (never read whole)."""
    with open_regular_file(path) as handle:
        data = handle.read(limit + 1)
    return data if len(data) <= limit else None


def manifest_bytes(path: Path) -> bytes:
    """A manifest's bytes, refused unread past the size of a whole signed index (ManifestError)."""
    data = read_capped(path, MAX_INDEX_BYTES)
    if data is None:
        raise ManifestError(f"{path.name} is larger than {MAX_INDEX_BYTES} bytes, more than any signed index holds")
    return data
