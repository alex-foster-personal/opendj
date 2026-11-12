"""Validation for precomputed Demucs four-stem artifact bundles.

Mini-PRD
========

``(private path)/<stable_id>/manifest.json`` is the only supported artifact
contract.  Version 1 has this exact shape::

    {
      "schema_version": 1,
      "stable_id": "canonical-track-id",
      "model": {"name": "htdemucs", "version": "4.0.1"},
      "source": {"path": "/music/source.flac", "sha256": "...64 hex..."},
      "files": {
        "vocals": "vocals.wav", "drums": "drums.wav",
        "bass": "bass.wav", "other": "other.wav"
      }
    }

* [if] every declared part is a regular WAV within its own bundle [then \u26d4]
  the reader exposes it only after metadata validation.
* [if] sample rate, frame count, or channels differ across parts [then \u26d4]
  the bundle is rejected instead of reporting a false, playable stem set.
* [if] a manifest, stable id, model, source provenance, or file entry is
  malformed [then \u26d4] validation fails explicitly; this module never
  generates, repairs, or substitutes audio.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
import time
from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from apps.shared.paths import STATE_DIR
from apps.shared.stable_id import is_safe_stable_id_segment
from apps.stems import artifacts_flac as _flac_md
from apps.stems import mp3_metadata as _mp3_md
from apps.stems.artifact_types import StemArtifactError, WavMetadata
from apps.stems.artifacts_flac import _require_flac_magic, read_flac_metadata
from apps.stems.constants import STEM_MANIFEST_FILENAME
from apps.stems.mp3_metadata import (
    _looks_like_mp3,
    _read_mp3_probe,
    read_mp3_metadata,
    scan_mp3_frames,
)
from apps.stems.wav_metadata import read_wav_metadata

# Bind submodule helpers on this module for tests that import from
# apps.stems.artifacts (not part of __all__).
_flac_block_size_from_header = _flac_md._flac_block_size_from_header
_flac_valid_frame_header_at = _flac_md._flac_valid_frame_header_at
_find_mp3_sync = _mp3_md._find_mp3_sync
_id3v2_skip_size = _mp3_md._id3v2_skip_size
_validate_mp3_xing_tail = _mp3_md._validate_mp3_xing_tail

LOGGER = logging.getLogger(__name__)

# MP3 encoder padding can shift frame counts slightly between parts; the deck
# mixes the overlap, never invented samples (same contract as register_stems).
FRAME_MISMATCH_TOL_S = 0.1

STEM_PARTS: tuple[str, str, str, str] = ("vocals", "drums", "bass", "other")
"""The complete standard Demucs 4-part output, in API presentation order."""

ROFORMER_PARTS: tuple[str, str] = ("vocals", "instrumental")
"""Mel-Band RoFormer's 2-part output, in API presentation order.

This is a DIFFERENT SPLIT, not a subset of the Demucs four: ``instrumental``
is everything that is not vocals, so it already contains the drums and bass
that Demucs would have separated. A deck driven by this layout can therefore
mute/solo VOCAL and INSTRUMENTAL truthfully, and CANNOT offer DRUMS at all --
there is no drums signal to gain to zero. The DRUMS control must render inert
for these bundles rather than silently doing nothing.
"""

STEM_LAYOUTS: dict[str, tuple[str, ...]] = {
    "demucs4": STEM_PARTS,
    "roformer2": ROFORMER_PARTS,
}

StemPart = Literal["vocals", "drums", "bass", "other", "instrumental"]
# The stable-id segment rule now lives in apps.shared.stable_id; see
# validate_stable_id below for why it had to move out of this module.
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MEDIA_TYPES: dict[str, str] = {
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
}
_STEM_SUFFIXES = set(_MEDIA_TYPES)

DEFAULT_STEMS_DIR = STATE_DIR / "stems"
PRODUCTION_ROFORMER_STEMS_DIR = STATE_DIR / "stems-roformer-spike"
"""Production RoFormer 2-part bundles (``roformer2`` layout).

The on-disk directory name still ends in ``-spike`` from the first farm pass;
promoting the tree is tracked on parent issue #5814. Kept as a SEPARATE root
rather than merged into ``stems/`` because the two stores are produced by
different models at different times and a stable_id can legitimately have a
bundle in both. Lookup order (see ``stem_roots``) decides which one a deck gets,
and keeping them apart means re-running either farm never overwrites the
other's output.
"""


def stem_roots(primary: Path | None = None) -> tuple[Path, ...]:
    """Stem stores to search, in precedence order.

    Demucs 4-part first: it drives more controls (DRUMS included), so where a
    track has both, the richer bundle wins.
    """
    first = Path(primary) if primary is not None else DEFAULT_STEMS_DIR
    return (first, PRODUCTION_ROFORMER_STEMS_DIR) if first != PRODUCTION_ROFORMER_STEMS_DIR else (first,)


def stems_accept_v2() -> bool:
    """Temporary farm-play toggle: accept schema v2 FLAC bundles (htdemucs_ft).

    Default ON so freshly landed farm artifacts are playable. Set
    ``MDT_STEMS_ACCEPT_V2=0`` to enforce strict v1 WAV-only again.
    """
    raw = os.getenv("MDT_STEMS_ACCEPT_V2", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


class StemBundleNotFoundError(FileNotFoundError):
    """No precomputed bundle exists for the requested stable id."""


class DemucsModel(BaseModel):
    """The specific standard four-part Demucs model that produced a bundle."""

    model_config = ConfigDict(extra="forbid", strict=True)

    # Farm v2 also lands htdemucs_ft; keep the field a short non-empty name.
    name: Annotated[str, Field(min_length=1, max_length=64)]
    version: Annotated[str, Field(min_length=1, max_length=64)]


class StemSourceProvenance(BaseModel):
    """Immutable source identity recorded when the stems were generated."""

    model_config = ConfigDict(extra="forbid", strict=True)

    path: Annotated[str, Field(min_length=1, max_length=4096)]
    sha256: Annotated[str, Field(min_length=64, max_length=64)]

    @field_validator("path")
    @classmethod
    def require_non_blank_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source.path must not be blank")
        return value

    @field_validator("sha256")
    @classmethod
    def require_sha256_hex(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("source.sha256 must be exactly 64 hexadecimal characters")
        return value.lower()


class StemManifest(BaseModel):
    """Strict, versioned JSON manifest stored alongside one Demucs bundle."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    stable_id: Annotated[str, Field(min_length=1, max_length=128)]
    model: DemucsModel
    source: StemSourceProvenance
    files: dict[StemPart, Annotated[str, Field(min_length=1, max_length=512)]]
    # Defaulted so every already-written v1 manifest on disk stays valid.
    layout: Literal["demucs4", "roformer2"] = "demucs4"

    @field_validator("stable_id")
    @classmethod
    def require_safe_stable_id(cls, value: str) -> str:
        validate_stable_id(value)
        return value

    @model_validator(mode="after")
    def require_exact_layout_parts(self) -> StemManifest:
        expected = STEM_LAYOUTS[self.layout]
        if set(self.files) != set(expected):
            raise ValueError(f"files must contain exactly {expected!r} for {self.layout}")
        if len(set(self.files.values())) != len(expected):
            raise ValueError("each stem part must declare a distinct file")
        return self


@dataclass(frozen=True)
class StemBundle:
    """A validated manifest, regular stem file paths, and shared metadata."""

    manifest: StemManifest
    files: dict[StemPart, Path]
    alignment: WavMetadata
    media_type: str = "audio/wav"
    layout: str = "demucs4"

    @property
    def parts(self) -> tuple[str, ...]:
        """The parts this bundle actually has -- NOT a fixed four."""
        return STEM_LAYOUTS[self.layout]


# ---------------------------------------------------------------------------
# Manifest and filesystem validation
# ---------------------------------------------------------------------------


def validate_stable_id(stable_id: str) -> None:
    """Reject IDs that could select a different artifact directory.

    The rule itself moved to ``apps.shared.stable_id`` because the stems JOB
    has to apply the identical rule at enqueue time and cannot import this
    module without a domain->webui cycle. This stays as the raiser so every
    existing caller keeps catching ``StemArtifactError``.
    """
    if not is_safe_stable_id_segment(stable_id):
        raise StemArtifactError("stable_id must use 1-128 URL-safe identifier characters")


def load_stem_bundle(
    stable_id: str,
    *,
    stems_dir: Path = DEFAULT_STEMS_DIR,
    roots: Sequence[Path] | None = None,
) -> StemBundle:
    """Load one complete, aligned bundle from the first valid root that has it.

    Roots are tried in ``stem_roots`` order. An invalid bundle is logged and
    does not shadow a valid lower-precedence bundle. If no candidate validates,
    the raised error identifies every configured root and each failed candidate.
    """
    validate_stable_id(stable_id)
    search_roots = tuple(Path(root) for root in roots) if roots is not None else stem_roots(stems_dir)
    if not search_roots:
        raise StemArtifactError("at least one stem root is required")
    failures: list[tuple[Path, StemArtifactError]] = []
    for root in search_roots:
        candidate = root.resolve() / stable_id
        if candidate.is_dir() and not candidate.is_symlink():
            try:
                return _load_from_root(stable_id, root.resolve())
            except StemArtifactError as exc:
                failures.append((root, exc))
                LOGGER.warning(
                    "invalid stem bundle skipped for fallback: stable_id=%s root=%s error=%s",
                    stable_id,
                    root,
                    exc,
                )
    roots_message = ", ".join(str(root) for root in search_roots)
    if failures:
        failures_message = "; ".join(f"{root}: {exc}" for root, exc in failures)
        raise StemArtifactError(
            f"no valid stem bundle exists for {stable_id!r} in any of "
            f"{roots_message}; failed candidates: {failures_message}"
        )
    raise StemBundleNotFoundError(f"no stem bundle exists for {stable_id!r} in any of {roots_message}")


def _load_from_root(stable_id: str, root: Path) -> StemBundle:
    bundle_dir = root / stable_id
    if not bundle_dir.is_dir() or bundle_dir.is_symlink():
        raise StemBundleNotFoundError(f"no stem bundle exists for {stable_id!r}")
    resolved_bundle = bundle_dir.resolve()
    try:
        resolved_bundle.relative_to(root)
    except ValueError as exc:
        raise StemArtifactError("stem bundle resolves outside the configured stems directory") from exc

    manifest_path = bundle_dir / "manifest.json"
    _require_regular_file(manifest_path, label="manifest.json")
    raw = read_manifest_raw(manifest_path)
    schema = raw.get("schema_version")
    if schema == 2:
        if not stems_accept_v2():
            raise StemArtifactError("manifest.json is stem schema v2 (farm FLAC); set MDT_STEMS_ACCEPT_V2=1 to play")
        return _load_v2_bundle(stable_id, resolved_bundle, raw)
    if schema == 3:
        return _load_v3_bundle(stable_id, resolved_bundle, raw)
    if schema != 1:
        raise StemArtifactError(f"unsupported stem schema_version {schema!r}")
    return _load_v1_bundle(stable_id, resolved_bundle, raw)


def read_manifest_raw(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StemArtifactError(f"manifest.json is unreadable JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise StemArtifactError("manifest.json must be a JSON object")
    return raw


def _load_v1_bundle(stable_id: str, bundle_dir: Path, raw: dict) -> StemBundle:
    try:
        manifest = StemManifest.model_validate(raw)
    except ValidationError as exc:
        raise StemArtifactError(f"manifest.json violates stem schema v1: {exc}") from exc
    if manifest.stable_id != stable_id:
        raise StemArtifactError(f"manifest stable_id {manifest.stable_id!r} does not match requested {stable_id!r}")

    files: dict[StemPart, Path] = {}
    metadata: dict[StemPart, WavMetadata] = {}
    containers: dict[StemPart, str] = {}
    for part in STEM_LAYOUTS[manifest.layout]:
        file_path = _resolve_stem_file(bundle_dir, manifest.files[part], part)
        files[part] = file_path
        part_meta, container = read_stem_container_metadata(file_path)
        metadata[part] = part_meta
        containers[part] = container

    unique_containers = set(containers.values())
    if len(unique_containers) != 1:
        raise StemArtifactError(
            "v1 bundle mixes containers "
            f"{ {part: containers[part] for part in containers} }; "
            "every part must share one codec"
        )
    container = unique_containers.pop()
    media_type = _MEDIA_TYPES[container]

    parts = STEM_LAYOUTS[manifest.layout]
    reference = metadata[parts[0]]
    for part in parts[1:]:
        _validate_v1_part_metadata(
            reference,
            metadata[part],
            reference_part=parts[0],
            part=part,
            container=container,
        )
    min_frames = min(metadata[part].frame_count for part in parts)
    alignment = WavMetadata(
        sample_rate=reference.sample_rate,
        frame_count=min_frames,
        channels=reference.channels,
    )
    return StemBundle(
        manifest=manifest,
        files=files,
        alignment=alignment,
        media_type=media_type,
        layout=manifest.layout,
    )


def _load_v2_bundle(stable_id: str, bundle_dir: Path, raw: dict) -> StemBundle:
    """Farm schema v2: FLAC parts + nested model/audio; project into v1 client shape."""
    if raw.get("stable_id") != stable_id:
        raise StemArtifactError(f"manifest stable_id {raw.get('stable_id')!r} does not match requested {stable_id!r}")
    model_obj = raw.get("model")
    if not isinstance(model_obj, dict) or not isinstance(model_obj.get("name"), str):
        raise StemArtifactError("v2 manifest model.name must be a string")
    model_name = model_obj["name"].strip()
    model_version = str(model_obj.get("version") or "unknown").strip() or "unknown"
    source = raw.get("source")
    if not isinstance(source, dict):
        raise StemArtifactError("v2 manifest source must be an object")
    files_raw = raw.get("files")
    if not isinstance(files_raw, dict) or set(files_raw) != set(STEM_PARTS):
        raise StemArtifactError(f"v2 files must contain exactly {STEM_PARTS!r}")
    audio = raw.get("audio")
    if not isinstance(audio, dict):
        raise StemArtifactError("v2 manifest audio must be an object")
    try:
        alignment = WavMetadata(
            sample_rate=int(audio["sample_rate"]),
            frame_count=int(audio["frame_count"]),
            channels=int(audio["channels"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StemArtifactError(f"v2 manifest audio metadata incomplete: {exc}") from exc
    if alignment.sample_rate <= 0 or alignment.frame_count <= 0 or alignment.channels <= 0:
        raise StemArtifactError("v2 manifest audio metadata must be positive")

    files: dict[StemPart, Path] = {}
    for part in STEM_PARTS:
        declared = files_raw[part]
        if not isinstance(declared, str):
            raise StemArtifactError(f"v2 files.{part} must be a string path")
        file_path = _resolve_stem_file(bundle_dir, declared, part)
        _require_flac_magic(file_path)
        files[part] = file_path

    # Project into the existing StemManifest type for API callers.
    try:
        manifest = StemManifest.model_validate(
            {
                "schema_version": 1,
                "stable_id": stable_id,
                "model": {"name": model_name, "version": model_version},
                "source": {
                    "path": str(source.get("path") or "unknown"),
                    "sha256": str(source.get("sha256") or ("0" * 64)),
                },
                "files": {part: Path(files[part].name).name for part in STEM_PARTS},
            }
        )
    except ValidationError as exc:
        raise StemArtifactError(f"v2 manifest could not project to client contract: {exc}") from exc
    return StemBundle(manifest=manifest, files=files, alignment=alignment, media_type="audio/flac")


def _load_v3_bundle(stable_id: str, bundle_dir: Path, raw: dict) -> StemBundle:
    """Schema v3: an explicit ``layout`` and a matching part set, any codec.

    v1 and v2 both hard-code the Demucs four. v3 exists so a bundle can DECLARE
    which split it is, which is the whole point for RoFormer: two parts, one of
    them named ``instrumental``, and no drums signal in the box at all.

    Alignment comes from the manifest's ``audio`` block rather than being read
    out of the files, because MP3 has no cheap frame-exact header count. The
    generator is what measures it (ffprobe), so a wrong number here is a
    generator bug and shows up as a decode mismatch in the deck, not as a
    silently misaligned mix.
    """
    if raw.get("stable_id") != stable_id:
        raise StemArtifactError(f"manifest stable_id {raw.get('stable_id')!r} does not match requested {stable_id!r}")
    layout = raw.get("layout")
    if layout not in STEM_LAYOUTS:
        raise StemArtifactError(f"v3 manifest layout must be one of {sorted(STEM_LAYOUTS)}; got {layout!r}")
    parts = STEM_LAYOUTS[layout]

    model_obj = raw.get("model")
    if not isinstance(model_obj, dict) or not isinstance(model_obj.get("name"), str):
        raise StemArtifactError("v3 manifest model.name must be a string")
    source = raw.get("source")
    if not isinstance(source, dict):
        raise StemArtifactError("v3 manifest source must be an object")
    files_raw = raw.get("files")
    if not isinstance(files_raw, dict) or set(files_raw) != set(parts):
        raise StemArtifactError(f"v3 files must contain exactly {parts!r} for {layout}")

    audio = raw.get("audio")
    if not isinstance(audio, dict):
        raise StemArtifactError("v3 manifest audio must be an object")
    try:
        alignment = WavMetadata(
            sample_rate=int(audio["sample_rate"]),
            frame_count=int(audio["frame_count"]),
            channels=int(audio["channels"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StemArtifactError(f"v3 manifest audio metadata incomplete: {exc}") from exc
    if alignment.sample_rate <= 0 or alignment.frame_count <= 0 or alignment.channels <= 0:
        raise StemArtifactError("v3 manifest audio metadata must be positive")

    files, media_types = _resolve_v3_files(bundle_dir, files_raw, parts)

    try:
        manifest = StemManifest.model_validate(
            {
                "schema_version": 1,
                "stable_id": stable_id,
                "layout": layout,
                "model": {
                    "name": model_obj["name"].strip(),
                    "version": str(model_obj.get("version") or "unknown").strip() or "unknown",
                },
                "source": {
                    "path": str(source.get("path") or "unknown"),
                    "sha256": str(source.get("sha256") or ("0" * 64)),
                },
                "files": {part: files[part].name for part in parts},  # type: ignore[index]
            }
        )
    except ValidationError as exc:
        raise StemArtifactError(f"v3 manifest could not project to client contract: {exc}") from exc
    return StemBundle(
        manifest=manifest,
        files=files,
        alignment=alignment,
        media_type=media_types.pop(),
        layout=layout,
    )


def _resolve_v3_files(
    bundle_dir: Path, files_raw: dict[Any, Any], parts: tuple[str, ...]
) -> tuple[dict[StemPart, Path], set[str]]:
    """Resolve v3 parts and require one codec across the declared layout."""
    files: dict[StemPart, Path] = {}
    media_types: set[str] = set()
    for part in parts:
        declared = files_raw[part]
        if not isinstance(declared, str):
            raise StemArtifactError(f"v3 files.{part} must be a string path")
        file_path = _resolve_stem_file(bundle_dir, declared, part)
        files[part] = file_path  # type: ignore[index]
        media_types.add(_MEDIA_TYPES[file_path.suffix.lower()])
    if len(media_types) != 1:
        raise StemArtifactError(f"v3 bundle mixes codecs {sorted(media_types)}; every part must share one")
    return files, media_types


_LAYOUT_PART_STEM_NAMES: frozenset[str] = frozenset(part for parts in STEM_LAYOUTS.values() for part in parts)

StemArtifactDisposition = Literal["required", "stale"]


def classify_bundle_top_level_artifact(
    bundle_dir: Path,
    artifact_name: str,
    *,
    manifest: StemManifest,
) -> StemArtifactDisposition:
    """Classify one top-level bundle entry for cleanup planning (read-only; never deletes).

    Separates required manifest inputs and carry-over sidecars from stale regenerable
    stem-part files left after codec swaps. Planners use this name/manifest logic only;
    this module still never deletes or repairs audio.
    """
    _ = bundle_dir
    declared_basenames = set(manifest.files.values())
    if artifact_name == STEM_MANIFEST_FILENAME:
        return "required"
    if artifact_name in declared_basenames:
        return "required"
    if Path(artifact_name).stem in _LAYOUT_PART_STEM_NAMES:
        return "stale"
    return "required"


def resolve_manifest_stem_path(bundle_dir: Path, declared_name: str, part: StemPart) -> Path:
    """Resolve a manifest-declared stem path within its bundle root."""
    return _resolve_stem_file(bundle_dir, declared_name, part)


def _resolve_stem_file(bundle_dir: Path, declared_name: str, part: StemPart) -> Path:
    declared_path = Path(declared_name)
    suffix = declared_path.suffix.lower()
    if (
        declared_path.is_absolute()
        or PureWindowsPath(declared_name).is_absolute()
        or ".." in declared_path.parts
        or suffix not in _STEM_SUFFIXES
    ):
        raise StemArtifactError(f"{part} file entry must be a relative .wav/.flac/.mp3 path within its bundle")
    candidate = bundle_dir / declared_path
    _require_regular_file(candidate, label=f"{part} stem")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(bundle_dir)
    except ValueError as exc:
        raise StemArtifactError(f"{part} stem resolves outside its bundle") from exc
    return resolved


def _require_regular_file(path: Path, *, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise StemArtifactError(f"{label} is missing or unreadable") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise StemArtifactError(f"{label} must be a real regular file, not a symlink or directory")


# ---------------------------------------------------------------------------
# Container metadata validation (v1 parts: WAV, FLAC, MP3)
# ---------------------------------------------------------------------------


def read_stem_container_metadata(path: Path) -> tuple[WavMetadata, str]:
    """Read one stem part's geometry from its declared suffix and magic bytes."""
    suffix = path.suffix.lower()
    if suffix not in _STEM_SUFFIXES:
        raise StemArtifactError(f"{path.name} must use a supported stem suffix {sorted(_STEM_SUFFIXES)}")
    try:
        with path.open("rb") as source:
            magic = source.read(12)
    except OSError as exc:
        raise StemArtifactError(f"cannot read container header for {path.name}: {exc}") from exc

    if suffix == ".wav":
        if len(magic) < 12 or magic[:4] != b"RIFF" or magic[8:12] != b"WAVE":
            raise StemArtifactError(f"{path.name} suffix is .wav but content is not RIFF/WAVE")
        return read_wav_metadata(path), ".wav"
    if suffix == ".flac":
        if len(magic) < 4 or magic[:4] != b"fLaC":
            raise StemArtifactError(f"{path.name} suffix is .flac but content is not FLAC")
        return read_flac_metadata(path), ".flac"
    if suffix == ".mp3":
        probe = _read_mp3_probe(path, magic)
        if not _looks_like_mp3(probe):
            raise StemArtifactError(f"{path.name} suffix is .mp3 but content is not MPEG audio")
        return read_mp3_metadata(path), ".mp3"
    raise StemArtifactError(f"{path.name} uses unsupported container {suffix!r}")


def _validate_v1_part_metadata(
    reference: WavMetadata,
    candidate: WavMetadata,
    *,
    reference_part: str,
    part: str,
    container: str,
) -> None:
    """Require one part's geometry to match the fixed reference part."""
    if candidate.sample_rate != reference.sample_rate:
        raise StemArtifactError(
            f"{part} sample_rate {candidate.sample_rate} does not match "
            f"{reference_part} sample_rate {reference.sample_rate}"
        )
    if candidate.channels != reference.channels:
        raise StemArtifactError(
            f"{part} channels {candidate.channels} does not match {reference_part} channels {reference.channels}"
        )
    if container in {".wav", ".flac"}:
        if candidate.frame_count != reference.frame_count:
            raise StemArtifactError(
                f"{part} metadata {candidate!r} does not align with {reference_part} metadata {reference!r}"
            )
        return
    frame_gap = abs(candidate.frame_count - reference.frame_count)
    if frame_gap > FRAME_MISMATCH_TOL_S * reference.sample_rate:
        raise StemArtifactError(
            f"{part} frame_count {candidate.frame_count} differs from "
            f"{reference_part} frame_count {reference.frame_count} by {frame_gap} "
            f"frames (> {FRAME_MISMATCH_TOL_S}s at {reference.sample_rate} Hz)"
        )


# ---------------------------------------------------------------------------
# Listing summaries (browser "Stems" column)
# ---------------------------------------------------------------------------
#
# Deliberately NOT built on load_stem_bundle. That function is the playback
# gate: it validates every part, aligns geometry, and raises on anything it
# cannot vouch for -- correct before a deck sums four branches, far too strict
# and far too slow for a badge on 200 listing rows, where one malformed bundle
# must not blank the column for the whole page. This reads the manifest and
# stats the files, nothing more.

_STEM_GROUPS: dict[str, tuple[str, ...]] = {
    "V": ("vocals",),
    "D": ("drums",),
    # Everything that is not vocals or drums, under either layout. RoFormer's
    # single `instrumental` part and Demucs's bass+other both land here, which
    # is what makes one column readable across two different splits.
    "I": ("bass", "other", "instrumental"),
}


# Cheap as one summary is, build_track_rows asks for one per row and the All
# Tracks pane re-walks every row every LIBRARY_FALLBACK_POLL_MS (60 s): on an
# 8k-track library that is 8k manifest reads plus up to 4 stats each, every
# minute, per open pane. Cache per (root, stable_id) with an explicit bound.
#
# A TTL rather than an mtime revalidation because the common answer is "none"
# -- a track with no bundle has no manifest to stat, so only a clock can bound
# how long that answer is trusted, and one freshness contract beats two.
# 30 s mirrors config.FILE_EXISTS_TTL_S and is half the library poll, so a
# bundle that lands (or is deleted) is on screen by the next poll at worst.
STEM_SUMMARY_TTL_S: float = 30.0
_STEM_SUMMARY_LOCK = threading.Lock()
_STEM_SUMMARY_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def summarize_stem_bundle(stable_id: str, *, stems_dir: Path = DEFAULT_STEMS_DIR) -> dict[str, Any]:
    """A cheap listing badge for one bundle, cached for STEM_SUMMARY_TTL_S.

    Each caller gets its own copy: summaries are handed to row serializers
    that must not be able to reach back into the cache.
    """
    key = (str(stems_dir), stable_id)
    now = time.monotonic()
    with _STEM_SUMMARY_LOCK:
        hit = _STEM_SUMMARY_CACHE.get(key)
    if hit is not None and now - hit[0] < STEM_SUMMARY_TTL_S:
        return deepcopy(hit[1])
    summary = _read_stem_summary(stable_id, stems_dir)
    with _STEM_SUMMARY_LOCK:
        _STEM_SUMMARY_CACHE[key] = (now, summary)
    return deepcopy(summary)


def _summary_files(raw: Any) -> dict[str, Any]:
    """Extract summary file declarations with the same object contract as the reader."""
    if not isinstance(raw, dict):
        raise TypeError("manifest.json must be an object")
    files = raw.get("files")
    if not isinstance(files, dict):
        raise TypeError("files must be an object")
    return files


def _read_stem_summary(stable_id: str, stems_dir: Path) -> dict[str, Any]:
    """Read one bundle off disk: ``{"status": "none"}`` or ready.

    Never raises for a bad bundle -- an unreadable or malformed manifest is
    reported as ``none``, the same as absent, because the listing's job is to
    say "there is nothing playable here", not to diagnose why.
    """
    try:
        validate_stable_id(stable_id)
    except StemArtifactError:
        return {"status": "none"}

    for root in stem_roots(stems_dir):
        bundle_dir = Path(root) / stable_id
        manifest_path = bundle_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            files = _summary_files(raw)
        except (OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            # "invalid" is NOT "none": a bundle whose manifest is corrupt is a
            # thing to go and fix, and the column says so rather than showing
            # the same blank as a track that was simply never separated.
            return {"status": "invalid", "error": f"{type(exc).__name__}: {exc}"}

        groups: dict[str, dict[str, Any]] = {}
        total = 0
        suffixes: set[str] = set()
        for label, candidates in _STEM_GROUPS.items():
            group_bytes = 0
            present: list[str] = []
            for part in candidates:
                name = files.get(part)
                if not isinstance(name, str):
                    continue
                path = bundle_dir / name
                try:
                    group_bytes += path.stat().st_size
                except OSError:
                    continue
                present.append(part)
                suffixes.add(path.suffix.lower().lstrip("."))
            # `parts` names WHICH stems back this group, so a 2-part RoFormer
            # "I" (instrumental) is distinguishable from a Demucs "I"
            # (bass+other) in the UI without guessing from the byte count.
            groups[label] = {"bytes": group_bytes, "parts": present}
            total += group_bytes

        if not any(group["parts"] for group in groups.values()):
            listed = ", ".join(sorted({n for n in files.values() if isinstance(n, str)}))
            return {
                "status": "invalid",
                "error": f"every stem file listed in manifest.json is missing: {listed}",
            }

        model = raw.get("model") if isinstance(raw.get("model"), dict) else {}
        preset = raw.get("preset") if isinstance(raw.get("preset"), dict) else {}
        return {
            "status": "ready",
            "model": model.get("name"),
            "preset": preset.get("tag"),
            "overlap": preset.get("overlap"),
            "shifts": preset.get("shifts"),
            "format": next(iter(sorted(suffixes)), ""),
            "groups": groups,
            "total_bytes": total,
        }
    return {"status": "none"}


def stem_label_clip_api_path(stable_id: str, part: str, *, api_prefix: str = "/api/v1") -> str:
    """Relative API path for streaming one validated stem part in a label task."""
    validate_stable_id(stable_id)
    if part not in STEM_PARTS and part not in ROFORMER_PARTS:
        raise StemArtifactError(f"unsupported stem part for label clip: {part!r}")
    prefix = api_prefix.rstrip("/")
    return f"{prefix}/tracks/{stable_id}/stems/{part}"


def validate_label_stem_clip(
    stable_id: str,
    part: str,
    *,
    stems_dir: Path = DEFAULT_STEMS_DIR,
    roots: Sequence[Path] | None = None,
) -> StemBundle:
    """Load a bundle for a labelling clip; refuse when the part is missing or invalid."""
    bundle = load_stem_bundle(stable_id, stems_dir=stems_dir, roots=roots)
    if part not in bundle.files:
        raise StemArtifactError(f"bundle for {stable_id!r} has no part {part!r}")
    return bundle


def bulk_stem_summaries(stable_ids: Iterable[str], *, stems_dir: Path = DEFAULT_STEMS_DIR) -> dict[str, dict[str, Any]]:
    """``summarize_stem_bundle`` for a listing page, keyed by stable_id.

    Repeat pages and the 60 s library re-poll are served from that function's
    TTL cache, so a warm page touches the filesystem not at all.
    """
    return {sid: summarize_stem_bundle(sid, stems_dir=stems_dir) for sid in stable_ids}


__all__ = [
    "DEFAULT_STEMS_DIR",
    "ROFORMER_PARTS",
    "PRODUCTION_ROFORMER_STEMS_DIR",
    "STEM_LAYOUTS",
    "STEM_PARTS",
    "STEM_SUMMARY_TTL_S",
    "StemArtifactDisposition",
    "DemucsModel",
    "StemArtifactError",
    "StemBundle",
    "StemBundleNotFoundError",
    "StemManifest",
    "StemPart",
    "StemSourceProvenance",
    "WavMetadata",
    "bulk_stem_summaries",
    "classify_bundle_top_level_artifact",
    "load_stem_bundle",
    "read_manifest_raw",
    "read_flac_metadata",
    "read_mp3_metadata",
    "read_stem_container_metadata",
    "read_wav_metadata",
    "resolve_manifest_stem_path",
    "scan_mp3_frames",
    "stem_roots",
    "summarize_stem_bundle",
    "stem_label_clip_api_path",
    "validate_label_stem_clip",
    "validate_stable_id",
]
