"""Write a separated track's stem bundle locally, with no modal import.

WHY THIS EXISTS (issue #3421). The stems worker writes each bundle through
helpers that live in ``scripts/modal_vocal_farm.py``. That module does
``import modal`` at module scope, so even the RELAY transport, which never
talks to Modal from this machine and is what the installed app ships, could
not run without modal installed. The app ships no modal, on purpose.

These are MIRRORS of the farm's own definitions, not a replacement for them.
The farm keeps its copies because Modal imports that file inside a container
that has no ``apps`` package and builds the R2 manifest there. A mirror rots
silently, so ``tests/stems/test_bundle_publish_mirror.py`` reads the farm's
source (without importing modal) and fails on any drift: the preset table,
the baked models, the schema constants, and the manifest each one builds for
the same input.

-Claude
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ----- CFG (mirrors scripts/modal_vocal_farm.py) -----------------------------
BAKED_MODELS: tuple[str, ...] = ("htdemucs", "hdemucs_mmi", "htdemucs_ft")
DEMUCS_VERSION: str = "4.0.1"
STEM_PARTS: tuple[str, str, str, str] = ("vocals", "drums", "bass", "other")
STEM_BUNDLE_SCHEMA: int = 3
STEM_LAYOUT: str = "demucs4"
STEM_CODEC_BY_SOURCE: str = "by-source"


@dataclass(frozen=True)
class Preset:
    """One rung of the measured quality ladder; see the farm's ``Preset``."""

    tag: str
    model: str
    overlap: float
    shifts: int
    rung: int

    def stamp(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "model": self.model,
            "overlap": self.overlap,
            "shifts": self.shifts,
            "rung": self.rung,
        }


PRESETS: dict[str, Preset] = {
    "hdemucs_mmi-ov0.25": Preset("hdemucs_mmi-ov0.25", "hdemucs_mmi", 0.25, 0, 0),
    "hdemucs_mmi-ov0.1": Preset("hdemucs_mmi-ov0.1", "hdemucs_mmi", 0.1, 0, 0),
    "htdemucs-ov0.1": Preset("htdemucs-ov0.1", "htdemucs", 0.1, 0, 3),
    "htdemucs-ov0.25": Preset("htdemucs-ov0.25", "htdemucs", 0.25, 0, 4),
    "htdemucs-ov0.5": Preset("htdemucs-ov0.5", "htdemucs", 0.5, 0, 5),
    "htdemucs_ft-ov0.25": Preset("htdemucs_ft-ov0.25", "htdemucs_ft", 0.25, 0, 7),
    "htdemucs_ft-ov0.5": Preset("htdemucs_ft-ov0.5", "htdemucs_ft", 0.5, 0, 8),
}


# ----- presets ---------------------------------------------------------------


def resolve_preset(name: str) -> Preset:
    """The preset ``name``, refusing one the farm image has no weights for."""
    if name not in PRESETS:
        raise SystemExit(
            f"error: unknown preset {name!r}; known: {', '.join(sorted(PRESETS))}"
        )
    preset = PRESETS[name]
    if preset.model not in BAKED_MODELS:
        raise SystemExit(
            f"error: preset {name!r} needs model {preset.model!r}, which is not "
            f"baked into the farm image (baked: {', '.join(BAKED_MODELS)})"
        )
    return preset


def preset_for_tier(tier_key: str) -> Preset:
    """The preset behind an ``apps/stems/tiers.py`` rung."""
    from apps.stems.tiers import get_tier

    return resolve_preset(get_tier(tier_key).preset_tag)


# ----- the bundle ------------------------------------------------------------


def route_stem_codec(tier_key: str) -> str:
    """The stem codec the Modal and relay routes ask the farm for (STEM-75).

    A tier whose own codec is ``flac`` (tier L, the scored control arm) asks
    for FLAC explicitly and gets it for every source. Every other tier asks
    for ``by-source``: MP3 through the size ladder for an MP3/M4A source, FLAC
    for a lossless one, so no part outgrows the file it came from. A tier's
    ``opus`` codec is the farm CLI's bulk default and is not what these routes
    write: the deck reads FLAC or MP3 (apps/stems/artifacts.py ``_MEDIA_TYPES``).
    """
    from apps.stems.tiers import get_tier

    return "flac" if get_tier(tier_key).codec == "flac" else STEM_CODEC_BY_SOURCE


def route_stem_codec_for_source(tier_key: str, source_name: str) -> str:
    """``route_stem_codec(tier)``, refusing a source the farm cannot encode for.

    Both Modal routes call this BEFORE they dispatch (STEM-75): under
    ``by-source`` an extension with no output-format policy (``.ogg``,
    ``.aac``, ...) would otherwise reach the GPU function and fail there, with
    a container already bought. A FLAC tier takes any source the decoder
    reads, so it is not refused here. The farm keeps its own in-container
    check (``farm_stem_encode.resolve_stem_codec``) as the defensive copy.

    Raises ``apps.stems.stem_size_policy.UnknownSourceFormatError`` (a
    ``ValueError``) naming the known extensions.
    """
    from apps.stems.stem_size_policy import stem_output_codec_for_source_ext

    codec = route_stem_codec(tier_key)
    if codec == STEM_CODEC_BY_SOURCE:
        stem_output_codec_for_source_ext(Path(source_name).suffix)
    return codec


# The farm's ``separate_track`` parameters, in order. A mirror like the rest of
# this file: tests/stems/test_farm_route_codec.py reads the farm's source and
# fails if its signature drifts from this tuple.
SEPARATE_TRACK_PARAMS: tuple[str, ...] = (
    "audio_bytes",
    "stable_id",
    "name",
    "model_name",
    "overlap",
    "shifts",
    "stems_dest",
    "source_path",
    "preset_stamp",
    "r2_bucket",
    "stem_codec",
)


def separate_track_args(
    *,
    audio_bytes: bytes,
    stable_id: str,
    name: str,
    source_path: str,
    preset: Any,
    stem_codec: str,
) -> tuple[Any, ...]:
    """The positional arguments both routes hand the farm's ``separate_track``.

    ``stems_dest='local'`` returns the stem bytes to the calling process and
    ``r2_bucket`` is unused for that destination. ``preset`` is a farm or
    mirror ``Preset`` (``model``, ``overlap``, ``shifts``, ``stamp()``).
    Positional because ``.starmap`` takes tuples; ``SEPARATE_TRACK_PARAMS``
    names each slot.
    """
    args = (
        audio_bytes,
        stable_id,
        name,
        preset.model,
        preset.overlap,
        preset.shifts,
        "local",
        source_path,
        preset.stamp(),
        "",
        stem_codec,
    )
    if len(args) != len(SEPARATE_TRACK_PARAMS):
        raise RuntimeError(
            f"separate_track takes {len(SEPARATE_TRACK_PARAMS)} arguments, "
            f"built {len(args)}"
        )
    return args


def stem_manifest(
    stable_id: str,
    source_path: str,
    result: dict[str, Any],
    preset_stamp: dict[str, Any],
) -> dict[str, Any]:
    """Bundle manifest, schema 3; byte-for-byte the farm's ``_stem_manifest``."""
    return {
        "schema_version": STEM_BUNDLE_SCHEMA,
        "stable_id": stable_id,
        "layout": STEM_LAYOUT,
        "model": {"name": preset_stamp["model"], "version": DEMUCS_VERSION},
        "source": {
            "path": source_path,
            "sha256": result["source_sha256"],
        },
        "files": {part: f"{part}.{result['stem_ext']}" for part in STEM_PARTS},
        "audio": result["audio"],
        "codec": result["stem_codec"],
        "size_policy": result.get("size_policy"),
        "preset": preset_stamp,
    }


def publish_stems_local(
    stable_id: str,
    audio_path: Path,
    result: dict[str, Any],
    preset: Preset,
    data_dir: Path,
) -> tuple[int, float]:
    """Write one bundle where the webui reads it; the farm's ``_publish_stems_local``.

    Holds the bundle's cross-process lock (:mod:`apps.stems.bundle_lock`)
    for the swap, after the parts are staged, so a re-compress swap (STEM-58) or an R2 hydrate of
    the same id can never interleave with it. The bundle is written whole
    beside the stem root and swapped in (``bundle_lock.publish_bundle``), so
    a deck streaming a part never reads one truncated mid-write (Codex P1 on
    #6821). A republish in another codec (a FLAC bundle farmed again as MP3)
    keeps none of the old parts, so they neither keep their storage nor show
    up in ``scan_bundles()`` against the cloud index (STEM-75); any other
    file in the directory is carried over.
    """
    from apps.stems.bundle_lock import publish_bundle

    started = time.perf_counter()
    bundle = data_dir / "state" / "stems" / stable_id
    manifest_doc = stem_manifest(stable_id, str(audio_path), result, preset.stamp())
    ext = result["stem_ext"]
    written = 0

    def write(staging: Path) -> None:
        nonlocal written
        for part in STEM_PARTS:
            part_path = staging / f"{part}.{ext}"
            part_path.write_bytes(result["stems"][part])
            written += part_path.stat().st_size
        (staging / "manifest.json").write_text(
            json.dumps(manifest_doc, indent=2) + "\n", encoding="utf-8", newline="\n"
        )

    def replaced(entry: Path) -> bool:
        # Every old part goes, whatever its codec (a FLAC publish over a
        # STEM-58 MP3 bundle keeps no .mp3 part; Codex on #6820), and so
        # does the old manifest; any other top-level file is carried over.
        return entry.name == "manifest.json" or (entry.is_file() and entry.stem in STEM_PARTS)

    publish_bundle(bundle, write, replaced)
    return written, time.perf_counter() - started


__all__ = [
    "BAKED_MODELS",
    "DEMUCS_VERSION",
    "PRESETS",
    "SEPARATE_TRACK_PARAMS",
    "STEM_BUNDLE_SCHEMA",
    "STEM_CODEC_BY_SOURCE",
    "STEM_LAYOUT",
    "STEM_PARTS",
    "Preset",
    "preset_for_tier",
    "publish_stems_local",
    "resolve_preset",
    "route_stem_codec",
    "route_stem_codec_for_source",
    "separate_track_args",
    "stem_manifest",
]
