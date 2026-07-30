"""Validation for precomputed Demucs four-stem artifact bundles.

Mini-PRD
========

``data/state/stems/<stable_id>/manifest.json`` is the only supported artifact
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
import os
import re
import stat
import struct
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from apps.shared.paths import STATE_DIR

STEM_PARTS: tuple[str, str, str, str] = ("vocals", "drums", "bass", "other")
"""The complete standard Demucs 4-part output, in API presentation order."""

StemPart = Literal["vocals", "drums", "bass", "other"]
_STABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_STEM_SUFFIXES = {".wav", ".flac"}

DEFAULT_STEMS_DIR = STATE_DIR / "stems"


def stems_accept_v2() -> bool:
    """Temporary farm-play toggle: accept schema v2 FLAC bundles (htdemucs_ft).

    Default ON so freshly landed farm artifacts are playable. Set
    ``MDT_STEMS_ACCEPT_V2=0`` to enforce strict v1 WAV-only again.
    """
    raw = os.getenv("MDT_STEMS_ACCEPT_V2", "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


class StemArtifactError(ValueError):
    """An artifact violates the durable precomputed-stems contract."""


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

    @field_validator("stable_id")
    @classmethod
    def require_safe_stable_id(cls, value: str) -> str:
        validate_stable_id(value)
        return value

    @field_validator("files")
    @classmethod
    def require_exact_standard_parts(
        cls, value: dict[StemPart, str]
    ) -> dict[StemPart, str]:
        if set(value) != set(STEM_PARTS):
            raise ValueError(f"files must contain exactly {STEM_PARTS!r}")
        if len(set(value.values())) != len(STEM_PARTS):
            raise ValueError("each standard stem part must declare a distinct file")
        return value


@dataclass(frozen=True)
class WavMetadata:
    """WAV fields which must agree across all stem files."""

    sample_rate: int
    frame_count: int
    channels: int


@dataclass(frozen=True)
class StemBundle:
    """A validated manifest, regular stem file paths, and shared metadata."""

    manifest: StemManifest
    files: dict[StemPart, Path]
    alignment: WavMetadata
    media_type: str = "audio/wav"


# ---------------------------------------------------------------------------
# Manifest and filesystem validation
# ---------------------------------------------------------------------------


def validate_stable_id(stable_id: str) -> None:
    """Reject IDs that could select a different artifact directory."""
    if not _STABLE_ID_RE.fullmatch(stable_id) or stable_id in {".", ".."}:
        raise StemArtifactError(
            "stable_id must use 1-128 URL-safe identifier characters"
        )


def load_stem_bundle(
    stable_id: str, *, stems_dir: Path = DEFAULT_STEMS_DIR
) -> StemBundle:
    """Load one complete, aligned stem bundle (v1 WAV, or v2 FLAC when toggled)."""
    validate_stable_id(stable_id)
    root = Path(stems_dir).resolve()
    bundle_dir = root / stable_id
    if not bundle_dir.is_dir() or bundle_dir.is_symlink():
        raise StemBundleNotFoundError(f"no stem bundle exists for {stable_id!r}")
    resolved_bundle = bundle_dir.resolve()
    try:
        resolved_bundle.relative_to(root)
    except ValueError as exc:
        raise StemArtifactError(
            "stem bundle resolves outside the configured stems directory"
        ) from exc

    manifest_path = bundle_dir / "manifest.json"
    _require_regular_file(manifest_path, label="manifest.json")
    raw = _read_manifest_raw(manifest_path)
    schema = raw.get("schema_version")
    if schema == 2:
        if not stems_accept_v2():
            raise StemArtifactError(
                "manifest.json is stem schema v2 (farm FLAC); set MDT_STEMS_ACCEPT_V2=1 to play"
            )
        return _load_v2_bundle(stable_id, resolved_bundle, raw)
    if schema != 1:
        raise StemArtifactError(f"unsupported stem schema_version {schema!r}")
    return _load_v1_bundle(stable_id, resolved_bundle, raw)


def _read_manifest_raw(path: Path) -> dict:
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
        raise StemArtifactError(
            f"manifest.json violates stem schema v1: {exc}"
        ) from exc
    if manifest.stable_id != stable_id:
        raise StemArtifactError(
            f"manifest stable_id {manifest.stable_id!r} does not match requested {stable_id!r}"
        )

    files: dict[StemPart, Path] = {}
    metadata: dict[StemPart, WavMetadata] = {}
    for part in STEM_PARTS:
        file_path = _resolve_stem_file(bundle_dir, manifest.files[part], part)
        files[part] = file_path
        metadata[part] = read_wav_metadata(file_path)

    alignment = metadata[STEM_PARTS[0]]
    for part in STEM_PARTS[1:]:
        if metadata[part] != alignment:
            raise StemArtifactError(
                f"{part} metadata {metadata[part]!r} does not align with "
                f"{STEM_PARTS[0]} metadata {alignment!r}"
            )
    return StemBundle(
        manifest=manifest, files=files, alignment=alignment, media_type="audio/wav"
    )


def _load_v2_bundle(stable_id: str, bundle_dir: Path, raw: dict) -> StemBundle:
    """Farm schema v2: FLAC parts + nested model/audio; project into v1 client shape."""
    if raw.get("stable_id") != stable_id:
        raise StemArtifactError(
            f"manifest stable_id {raw.get('stable_id')!r} does not match requested {stable_id!r}"
        )
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
        raise StemArtifactError(
            f"v2 manifest audio metadata incomplete: {exc}"
        ) from exc
    if (
        alignment.sample_rate <= 0
        or alignment.frame_count <= 0
        or alignment.channels <= 0
    ):
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
        raise StemArtifactError(
            f"v2 manifest could not project to client contract: {exc}"
        ) from exc
    return StemBundle(
        manifest=manifest, files=files, alignment=alignment, media_type="audio/flac"
    )


def _require_flac_magic(path: Path) -> None:
    try:
        with path.open("rb") as source:
            magic = source.read(4)
    except OSError as exc:
        raise StemArtifactError(
            f"cannot read FLAC header for {path.name}: {exc}"
        ) from exc
    if magic != b"fLaC":
        raise StemArtifactError(f"{path.name} is not a FLAC file (missing fLaC magic)")


def _resolve_stem_file(bundle_dir: Path, declared_name: str, part: StemPart) -> Path:
    declared_path = Path(declared_name)
    suffix = declared_path.suffix.lower()
    if (
        declared_path.is_absolute()
        or PureWindowsPath(declared_name).is_absolute()
        or ".." in declared_path.parts
        or suffix not in _STEM_SUFFIXES
    ):
        raise StemArtifactError(
            f"{part} file entry must be a relative .wav/.flac path within its bundle"
        )
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
        raise StemArtifactError(
            f"{label} must be a real regular file, not a symlink or directory"
        )


# ---------------------------------------------------------------------------
# WAV metadata validation
# ---------------------------------------------------------------------------


def read_wav_metadata(path: Path) -> WavMetadata:
    """Read RIFF/WAVE metadata without decoding or altering the audio samples."""
    try:
        file_size = path.stat().st_size
        with path.open("rb") as source:
            header = source.read(12)
            if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
                raise StemArtifactError(f"{path.name} is not a RIFF/WAVE file")
            riff_size = struct.unpack("<I", header[4:8])[0]
            if riff_size + 8 != file_size:
                raise StemArtifactError(
                    f"{path.name} RIFF size does not match its actual file size"
                )
            return _parse_riff_wav_chunks(
                source, file_size=file_size, file_name=path.name
            )
    except OSError as exc:
        raise StemArtifactError(
            f"cannot read WAV metadata for {path.name}: {exc}"
        ) from exc


def _parse_riff_wav_chunks(source, *, file_size: int, file_name: str) -> WavMetadata:
    fmt: tuple[int, int, int, int] | None = None
    data_size = 0
    offset = 12
    while offset < file_size:
        source.seek(offset)
        chunk_header = source.read(8)
        if len(chunk_header) != 8:
            raise StemArtifactError(f"{file_name} has a truncated RIFF chunk header")
        chunk_id, chunk_size = (
            chunk_header[:4],
            struct.unpack("<I", chunk_header[4:])[0],
        )
        payload_start = offset + 8
        payload_end = payload_start + chunk_size
        if payload_end > file_size:
            raise StemArtifactError(f"{file_name} has a RIFF chunk beyond EOF")
        if chunk_id == b"fmt ":
            if fmt is not None:
                raise StemArtifactError(f"{file_name} contains more than one fmt chunk")
            if chunk_size < 16:
                raise StemArtifactError(f"{file_name} has an invalid fmt chunk")
            source.seek(payload_start)
            audio_format, channels, sample_rate, _byte_rate, block_align, _bits = (
                struct.unpack("<HHIIHH", source.read(16))
            )
            if audio_format not in {1, 3, 0xFFFE}:
                raise StemArtifactError(
                    f"{file_name} uses unsupported WAV format {audio_format}"
                )
            fmt = (channels, sample_rate, block_align, audio_format)
        elif chunk_id == b"data":
            data_size += chunk_size
        offset = payload_end + (chunk_size % 2)

    if fmt is None or data_size == 0:
        raise StemArtifactError(
            f"{file_name} must contain fmt and non-empty data chunks"
        )
    channels, sample_rate, block_align, _audio_format = fmt
    if channels <= 0 or sample_rate <= 0 or block_align <= 0 or data_size % block_align:
        raise StemArtifactError(f"{file_name} contains inconsistent WAV metadata")
    return WavMetadata(
        sample_rate=sample_rate,
        frame_count=data_size // block_align,
        channels=channels,
    )


__all__ = [
    "DEFAULT_STEMS_DIR",
    "STEM_PARTS",
    "DemucsModel",
    "StemArtifactError",
    "StemBundle",
    "StemBundleNotFoundError",
    "StemManifest",
    "StemPart",
    "StemSourceProvenance",
    "WavMetadata",
    "load_stem_bundle",
    "read_wav_metadata",
    "validate_stable_id",
]
