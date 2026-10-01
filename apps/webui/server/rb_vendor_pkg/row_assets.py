"""What one rekordbox-mapped listing row reads off the share root (LIBM-137).

A row's preview strip, PVDI vocal regions and cover-art verdict all come from
files under the Pioneer share root, found through the containment walk. This
module computes the three together and remembers the result per pair of vendor
path strings, valid while every file it looked at is unchanged (see
``row_hydration_cache.StatWitnessCache`` for why that is not a containment
cache). A warm 500-row page then costs about four ``lstat`` calls a row instead
of about seventeen opens.

The demucs vocal fallback is deliberately NOT part of the remembered value: a
vocal-cache entry landing later must show on the next page, so
``vocals_for_content`` consults it live on every call.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from apps.adapters.rekordbox import config
from apps.adapters.rekordbox import paths as rb_paths
from apps.adapters.rekordbox.models import RbRowMeta
from apps.shared import platform_paths
from apps.shared.platform_paths import AssetResolver

from .anlz import preview_strip, vocals_payload
from .row_hydration_cache import StatWitnessCache, WitnessingResolver


@dataclass(frozen=True)
class RowAssets:
    """The share-root facts of one mapped row."""

    preview_b64: str | None
    preview_max: int | None
    artwork_available: bool | None
    artwork_status: str
    #: PVDI vocals only (rekordbox / no_vocals / not_analyzed), before the
    #: demucs fallback. A fresh copy per caller: rows go to serializers.
    pvdi_vocals: dict[str, Any]


# Keyed by (share root, AnalysisDataPath, ImagePath). The share root is in the
# key because the same vendor strings name different files after a library-mode
# switch (``platform_paths.refresh_share_root``).
_ROW_ASSETS: StatWitnessCache[RowAssets] = StatWitnessCache()


def pvdi_vocals(
    analysis_data_path: str | None, *, resolver: AssetResolver | None = None
) -> dict[str, Any]:
    """The PVDI ``vocals`` payload from a row's .2EX, ``not_analyzed`` without one."""
    if analysis_data_path:
        mapped = rb_paths.resolve_asset_path(analysis_data_path, resolver=resolver)
        if mapped.resolved is not None:
            twoex = rb_paths._asset_sibling(
                mapped, mapped.resolved.with_suffix(".2EX"), resolver=resolver
            )
            if twoex.resolved is not None:
                return vocals_payload(twoex.resolved)
    return {"status": "not_analyzed"}


def rb_artwork_facts(
    meta: RbRowMeta, *, resolver: AssetResolver | None = None
) -> tuple[bool | None, str]:
    """Wire pair for a rekordbox-mapped row: its pre-rendered small cover."""
    if meta.image_path is None:
        return False, "no_image_path"
    mapped = rb_paths.resolve_asset_path(meta.image_path, resolver=resolver)
    if mapped.resolved is None:
        return False, "unresolved"
    artwork = mapped.resolved.parent / config.ARTWORK_FILENAMES["s"]
    if isinstance(resolver, WitnessingResolver):
        resolver.witness(artwork)
    if not artwork.is_file():
        return False, "file_missing"
    return True, "ok"


def rb_row_assets(meta: RbRowMeta, *, resolver: AssetResolver) -> RowAssets:
    """Preview strip, PVDI vocals and artwork verdict of one mapped row.

    ``resolver`` is the caller's per-call memo (pin ad59ac); it still dedupes
    rows that share a path on a cold page.
    """
    key = (
        str(platform_paths.SHARE_ROOT),
        meta.analysis_data_path or "",
        meta.image_path or "",
    )
    hit = _ROW_ASSETS.get(key)
    if hit is None:
        witnessing = WitnessingResolver(resolver)
        preview_b64, preview_max = preview_strip(meta.analysis_data_path, resolver=witnessing)
        artwork_available, artwork_status = rb_artwork_facts(meta, resolver=witnessing)
        hit = RowAssets(
            preview_b64=preview_b64,
            preview_max=preview_max,
            artwork_available=artwork_available,
            artwork_status=artwork_status,
            pvdi_vocals=pvdi_vocals(meta.analysis_data_path, resolver=witnessing),
        )
        _ROW_ASSETS.put(key, witnessing, hit)
    return RowAssets(
        preview_b64=hit.preview_b64,
        preview_max=hit.preview_max,
        artwork_available=hit.artwork_available,
        artwork_status=hit.artwork_status,
        pvdi_vocals=deepcopy(hit.pvdi_vocals),
    )
