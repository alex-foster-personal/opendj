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
STEM_BUNDLE_SCHEMA: int = 2


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


def stem_manifest(
    stable_id: str,
    source_path: str,
    result: dict[str, Any],
    preset_stamp: dict[str, Any],
) -> dict[str, Any]:
    """Bundle manifest, schema 2; byte-for-byte the farm's ``_stem_manifest``."""
    return {
        "schema_version": STEM_BUNDLE_SCHEMA,
        "stable_id": stable_id,
        "model": {"name": preset_stamp["model"], "version": DEMUCS_VERSION},
        "source": {
            "path": source_path,
            "sha256": result["source_sha256"],
        },
        "files": {
            part: f"{part}.{result.get('stem_ext', 'flac')}" for part in STEM_PARTS
        },
        "preset": preset_stamp,
        "audio": result["audio"],
    }


def publish_stems_local(
    stable_id: str,
    audio_path: Path,
    result: dict[str, Any],
    preset: Preset,
    data_dir: Path,
) -> tuple[int, float]:
    """Write one bundle where the webui reads it; the farm's ``_publish_stems_local``."""
    started = time.perf_counter()
    bundle = data_dir / "state" / "stems" / stable_id
    bundle.mkdir(parents=True, exist_ok=True)
    written = 0
    ext = result.get("stem_ext", "flac")
    for part in STEM_PARTS:
        part_path = bundle / f"{part}.{ext}"
        part_path.write_bytes(result["stems"][part])
        written += part_path.stat().st_size
    manifest = bundle / "manifest.json"
    manifest.write_text(
        json.dumps(
            stem_manifest(stable_id, str(audio_path), result, preset.stamp()),
            indent=2,
        )
        + "\n"
    )
    return written, time.perf_counter() - started


__all__ = [
    "BAKED_MODELS",
    "DEMUCS_VERSION",
    "PRESETS",
    "STEM_BUNDLE_SCHEMA",
    "STEM_PARTS",
    "Preset",
    "preset_for_tier",
    "publish_stems_local",
    "resolve_preset",
    "stem_manifest",
]
