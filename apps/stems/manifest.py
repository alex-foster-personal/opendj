"""Deck-consistent manifest document reads (publication stays in ``bundle_lock``)."""

from __future__ import annotations

from pathlib import Path

from apps.cloud.stem_cache_budget import read_barrier
from apps.stems.artifacts import _require_regular_file, read_manifest_raw

MANIFEST_FILENAME = "manifest.json"


def read_manifest_document(data_dir: Path, stable_id: str, bundle_dir: Path) -> dict:
    """Open ``bundle_dir``'s manifest under the deck read barrier with production parse rules."""
    bundle_dir = Path(bundle_dir)
    with read_barrier(data_dir, stable_id):
        manifest_path = bundle_dir / MANIFEST_FILENAME
        _require_regular_file(manifest_path, label=MANIFEST_FILENAME)
        return read_manifest_raw(manifest_path)
