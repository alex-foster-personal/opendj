"""Reversible vendor taxonomy map beside Beatport genre inference (ENRICH-12086).

The Cyanite category snapshot is a research fixture, not a vendored full
vocabulary. This module preserves every category and count from that snapshot,
keeps vendor spellings verbatim, and maps only through explicit Open DJ ids.

-Claude
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from . import taxonomy

OPEN_DJ_UNKNOWN = "unknown"
CYANITE_SNAPSHOT_REL = Path("(private path)")
MAP_VERSION = "enrichment-tag-map/v1"

# Explicit crosswalk entries only. Unknown vendor labels stay ``unknown`` with
# the raw spelling intact; never silently coerce (e.g. Agressive, Nonrythmic).
_EXPLICIT_VENDOR_TO_OPEN_DJ: dict[tuple[str, str], str] = {
    ("Simple Moods", "Agressive"): "mood.aggressive",
    ("Moods", "Aggressive"): "mood.aggressive",
    ("Movement", "Nonrythmic"): "movement.nonrhythmic",
    ("Movement", "Groovy"): "movement.groovy",
    ("Movement", "Driving"): "movement.driving",
}


class TagMapError(ValueError):
    """Snapshot or mapping input is invalid."""


@dataclass(frozen=True)
class TagResolution:
    vendor: str
    category: str
    vendor_value: str
    open_dj_id: str
    mapped: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "mapped": self.mapped,
            "open_dj_id": self.open_dj_id,
            "vendor": self.vendor,
            "vendor_value": self.vendor_value,
        }


@dataclass(frozen=True)
class TaxonomySnapshotStats:
    provider: str
    category_count: int
    tag_count: int
    map_version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "category_count": self.category_count,
            "map_version": self.map_version,
            "provider": self.provider,
            "tag_count": self.tag_count,
        }


@dataclass(frozen=True)
class TaxonomyMap:
    """In-memory map loaded from the Cyanite snapshot fixture."""

    stats: TaxonomySnapshotStats
    categories: tuple[dict[str, Any], ...]
    explicit: Mapping[tuple[str, str], str]

    def resolve(self, *, vendor: str, category: str, vendor_value: str) -> TagResolution:
        """Map one vendor tag; raw spelling is always returned unchanged."""
        if not vendor_value:
            raise TagMapError("vendor_value is required")
        key = (category, vendor_value)
        open_dj_id = self.explicit.get(key, OPEN_DJ_UNKNOWN)
        return TagResolution(
            vendor=vendor,
            category=category,
            vendor_value=vendor_value,
            open_dj_id=open_dj_id,
            mapped=open_dj_id != OPEN_DJ_UNKNOWN,
        )

    def category_names(self) -> tuple[str, ...]:
        return tuple(c["name"] for c in self.categories)

    def preserve_snapshot(self) -> dict[str, Any]:
        """Round-trip metadata for regression tests (32 categories / 5079 tags)."""
        return {
            "categories": [dict(c) for c in self.categories],
            "snapshot": self.stats.as_dict(),
        }


def repo_root() -> Path:
    """The music-dj-tools checkout that owns ``apps/genre_infer``."""
    return Path(__file__).resolve().parents[2]


def default_snapshot_path(root: Path | None = None) -> Path:
    base = repo_root() if root is None else root
    return base / CYANITE_SNAPSHOT_REL


def load_snapshot(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise TagMapError(f"taxonomy snapshot missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TagMapError(f"taxonomy snapshot is not JSON: {path}") from exc
    if "categories" not in payload or "snapshot" not in payload:
        raise TagMapError(f"taxonomy snapshot missing categories or snapshot block: {path}")
    return payload


def load_taxonomy_map(
    path: Path | None = None,
    *,
    explicit: Mapping[tuple[str, str], str] | None = None,
) -> TaxonomyMap:
    """Load the Cyanite snapshot and explicit crosswalk. No vendor account required."""
    snap_path = default_snapshot_path() if path is None else path
    payload = load_snapshot(snap_path)
    snap = payload["snapshot"]
    categories = tuple(payload["categories"])
    category_count = len(categories)
    tag_count = sum(int(c.get("count", 0)) for c in categories)
    claimed_cats = int(snap.get("public_claimed_category_count", -1))
    claimed_tags = int(snap.get("public_claimed_tag_count", -1))
    if claimed_cats >= 0 and category_count != claimed_cats:
        raise TagMapError(f"expected {claimed_cats} categories, snapshot has {category_count}")
    if claimed_tags >= 0 and tag_count != claimed_tags:
        raise TagMapError(f"expected {claimed_tags} tags, snapshot sums to {tag_count}")
    stats = TaxonomySnapshotStats(
        provider=str(snap.get("provider", "unknown")),
        category_count=category_count,
        tag_count=tag_count,
        map_version=MAP_VERSION,
    )
    merged = dict(_EXPLICIT_VENDOR_TO_OPEN_DJ)
    if explicit:
        merged.update(explicit)
    return TaxonomyMap(stats=stats, categories=categories, explicit=merged)


def resolve_beatport_genre(name: str) -> TagResolution:
    """Beatport taxonomy names map to themselves; unknown names stay explicit unknown."""
    canonical = taxonomy.canonical(name)
    open_dj_id = canonical if canonical is not None else OPEN_DJ_UNKNOWN
    return TagResolution(
        vendor="beatport",
        category="Main Genres",
        vendor_value=name,
        open_dj_id=open_dj_id,
        mapped=open_dj_id != OPEN_DJ_UNKNOWN,
    )


@lru_cache(maxsize=1)
def default_map() -> TaxonomyMap:
    return load_taxonomy_map()


def bpm_schema_discrepancy_note() -> str:
    """Human-facing note: explorer 60-280 vs REST BpmV2 60-200; importers bind schema_version."""
    return (
        "Cyanite taxonomy explorer documents BPM 60-280 while BpmV2 REST docs say 60-200; "
        "store the raw numeric value under the bound schema_version, never overwrite transport truth."
    )


__all__ = [
    "CYANITE_SNAPSHOT_REL",
    "MAP_VERSION",
    "OPEN_DJ_UNKNOWN",
    "TagMapError",
    "TagResolution",
    "TaxonomyMap",
    "TaxonomySnapshotStats",
    "bpm_schema_discrepancy_note",
    "default_map",
    "default_snapshot_path",
    "load_snapshot",
    "load_taxonomy_map",
    "repo_root",
    "resolve_beatport_genre",
]
