"""Stem waveform envelope helpers for stems routes.

Split from ``stems.py`` for issue #9676.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.stems.artifacts import StemArtifactError, StemBundleNotFoundError, load_stem_bundle

from .stems_bundle import _read_barrier, _stem_roots, _stems_dir


class StemWaveformOut(BaseModel):
    """Mono peak envelope for one stem part (issue #1036)."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    schema_version: int = Field(default=1, alias="schema")
    stable_id: str
    part: str
    layout: str
    points: int
    envelope: list[float]


def _stem_waveform_cache_root(request: Request) -> Path:
    configured = getattr(request.app.state, "data_dir", None)
    if configured is not None:
        return Path(configured) / "state" / "stem-waveform-cache"
    root = Path(os.environ.get("MDT_DATA_DIR", "data"))
    return root / "state" / "stem-waveform-cache"


# Resolves after the first that a waveform request makes when swaps keep
# landing under its decode; the last attempt's error is the answer.
_WAVEFORM_SWAP_RETRIES = 3


# What the read barrier saw: the bundle, the pin scratch dir (or None), the
# pinned links, the stat of every part the decode reads, and the envelope
# when nothing could be pinned and it was built under the barrier.
_ResolvedWaveform = tuple[Any, Path | None, dict[Path, Path], dict[Path, os.stat_result], dict[str, Any] | None]


def _retry_swaps(attempt: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run ``attempt`` again only when a swap landed under it, a bounded number of times.

    Only what a swap landing under the decode raises is retried: the
    builder's identity check, or a part path that vanished. A decode that
    fails or times out on its own fails once (Codex on #6821). The last
    attempt's error is the answer.
    """
    from apps.stems.stem_waveform import StemPartChangedError

    for _ in range(_WAVEFORM_SWAP_RETRIES):
        try:
            return attempt()
        except (StemPartChangedError, FileNotFoundError):
            continue
    return attempt()


def _resolve_waveform_inputs(
    stable_id: str, part: str, request: Request
) -> _ResolvedWaveform:
    """Resolve the bundle and pin the parts the decode reads, under :func:`_read_barrier`.

    Where the parts cannot be pinned the envelope is built here, still
    under the barrier: decoding the canonical paths after it, a swap and its
    rollback (which puts the same inodes back) could land between the bass
    and other decodes and leave every stat as it was, so a mixed envelope
    would pass the identity check and be cached (Sol on #6820). That holds
    writers off for the decode, on volumes that cannot hard-link only.
    """
    from apps.stems.stem_waveform import build_stem_waveform_payload, pin_waveform_inputs

    with _read_barrier(stable_id, request):
        bundle = load_stem_bundle(stable_id, stems_dir=_stems_dir(request), roots=_stem_roots(request))
        scratch, pinned, seen = pin_waveform_inputs(bundle, part)
        if scratch is None:
            payload = build_stem_waveform_payload(
                bundle, part, cache_root=_stem_waveform_cache_root(request), pinned={}, seen=seen
            )
            return bundle, None, {}, seen, payload
    return bundle, scratch, pinned, seen, None


def _build_resolved_waveform(
    resolved: _ResolvedWaveform,
    part: str,
    request: Request,
) -> dict[str, Any]:
    """Build the envelope from what :func:`_resolve_waveform_inputs` saw, then drop its pins.

    An unpinned resolve already built it under the barrier."""
    from apps.stems.stem_waveform import build_stem_waveform_payload, remove_waveform_pins

    bundle, scratch, pinned, seen, built = resolved
    if built is not None:
        return built
    try:
        return build_stem_waveform_payload(
            bundle, part, cache_root=_stem_waveform_cache_root(request), pinned=pinned, seen=seen
        )
    finally:
        if scratch is not None:
            remove_waveform_pins(scratch)  # raises StemPinCleanupError by name (Sol on #6820)


def _stem_waveform_payload(stable_id: str, part: str, request: Request) -> dict[str, Any]:
    """Resolve the bundle under :func:`_read_barrier`, then build the envelope outside it.

    A cold envelope decodes a whole part, too long to hold writers off, so
    the barrier covers only the resolve and hard-linking the parts the
    decode reads (``pin_waveform_inputs``). The decode then reads the
    version the barrier saw even if a swap lands under it, and an
    instrumental envelope never mixes two versions (Codex on #6820). Where
    the parts could not be linked, none is pinned, and the envelope is
    built under the barrier instead (Sol on #6820).
    """
    return _retry_swaps(
        lambda: _build_resolved_waveform(_resolve_waveform_inputs(stable_id, part, request), part, request)
    )


def _stem_waveform_response(stable_id: str, part: str, request: Request) -> StemWaveformOut:
    from apps.stems.stem_waveform import StemPinCleanupError

    try:
        payload = _stem_waveform_payload(stable_id, part, request)
    except StemPinCleanupError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "STEM_PIN_CLEANUP_FAILED", "message": str(exc)},
        ) from exc
    except StemBundleNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "STEM_BUNDLE_NOT_FOUND", "message": str(exc)},
        ) from exc
    except StemArtifactError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "STEM_PART_NOT_FOUND", "message": str(exc)},
        ) from exc
    return StemWaveformOut(**payload)
