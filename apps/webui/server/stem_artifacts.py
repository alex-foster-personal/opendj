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
import logging
import os
import re
import stat
import struct
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
ROFORMER_STEMS_DIR = STATE_DIR / "stems-roformer-spike"
"""Where the RoFormer farm lands its 2-part bundles.

Kept as a SEPARATE root rather than merged into ``stems/`` because the two
stores are produced by different models at different times and a stable_id can
legitimately have a bundle in both. Lookup order (see ``stem_roots``) decides
which one a deck gets, and keeping them apart means re-running either farm
never overwrites the other's output.
"""


def stem_roots(primary: Path | None = None) -> tuple[Path, ...]:
    """Stem stores to search, in precedence order.

    Demucs 4-part first: it drives more controls (DRUMS included), so where a
    track has both, the richer bundle wins.
    """
    first = Path(primary) if primary is not None else DEFAULT_STEMS_DIR
    return (first, ROFORMER_STEMS_DIR) if first != ROFORMER_STEMS_DIR else (first,)


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
        raise StemArtifactError(
            "stable_id must use 1-128 URL-safe identifier characters"
        )


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
    search_roots = (
        tuple(Path(root) for root in roots)
        if roots is not None
        else stem_roots(stems_dir)
    )
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
        failures_message = "; ".join(
            f"{root}: {exc}" for root, exc in failures
        )
        raise StemArtifactError(
            f"no valid stem bundle exists for {stable_id!r} in any of "
            f"{roots_message}; failed candidates: {failures_message}"
        )
    raise StemBundleNotFoundError(
        f"no stem bundle exists for {stable_id!r} in any of "
        f"{roots_message}"
    )


def _load_from_root(stable_id: str, root: Path) -> StemBundle:
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
    if schema == 3:
        return _load_v3_bundle(stable_id, resolved_bundle, raw)
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
    alignment = metadata[parts[0]]
    for part in parts[1:]:
        alignment = _align_v1_part_metadata(
            alignment,
            metadata[part],
            reference_part=parts[0],
            part=part,
            container=container,
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
        raise StemArtifactError(
            f"manifest stable_id {raw.get('stable_id')!r} does not match requested {stable_id!r}"
        )
    layout = raw.get("layout")
    if layout not in STEM_LAYOUTS:
        raise StemArtifactError(
            f"v3 manifest layout must be one of {sorted(STEM_LAYOUTS)}; got {layout!r}"
        )
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
                    "version": str(model_obj.get("version") or "unknown").strip()
                    or "unknown",
                },
                "source": {
                    "path": str(source.get("path") or "unknown"),
                    "sha256": str(source.get("sha256") or ("0" * 64)),
                },
                "files": {part: files[part].name for part in parts},  # type: ignore[index]
            }
        )
    except ValidationError as exc:
        raise StemArtifactError(
            f"v3 manifest could not project to client contract: {exc}"
        ) from exc
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
        raise StemArtifactError(
            f"v3 bundle mixes codecs {sorted(media_types)}; every part must share one"
        )
    return files, media_types


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
            f"{part} file entry must be a relative .wav/.flac/.mp3 path within its bundle"
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
# Container metadata validation (v1 parts: WAV, FLAC, MP3)
# ---------------------------------------------------------------------------

_MPEG1_L3_BITRATES_KBPS = (
    0,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    160,
    192,
    224,
    256,
    320,
)
_MPEG2_L3_BITRATES_KBPS = (
    0,
    8,
    16,
    24,
    32,
    40,
    48,
    56,
    64,
    80,
    96,
    112,
    128,
    144,
    160,
)
_MPEG_SAMPLE_RATES = {
    3: (44_100, 48_000, 32_000),
    2: (22_050, 24_000, 16_000),
    0: (11_025, 12_000, 8_000),
}


def read_stem_container_metadata(path: Path) -> tuple[WavMetadata, str]:
    """Read one stem part's geometry from its declared suffix and magic bytes."""
    suffix = path.suffix.lower()
    if suffix not in _STEM_SUFFIXES:
        raise StemArtifactError(
            f"{path.name} must use a supported stem suffix {sorted(_STEM_SUFFIXES)}"
        )
    try:
        with path.open("rb") as source:
            magic = source.read(12)
    except OSError as exc:
        raise StemArtifactError(
            f"cannot read container header for {path.name}: {exc}"
        ) from exc

    if suffix == ".wav":
        if len(magic) < 12 or magic[:4] != b"RIFF" or magic[8:12] != b"WAVE":
            raise StemArtifactError(
                f"{path.name} suffix is .wav but content is not RIFF/WAVE"
            )
        return read_wav_metadata(path), ".wav"
    if suffix == ".flac":
        if len(magic) < 4 or magic[:4] != b"fLaC":
            raise StemArtifactError(
                f"{path.name} suffix is .flac but content is not FLAC"
            )
        return read_flac_metadata(path), ".flac"
    if suffix == ".mp3":
        probe = _read_mp3_probe(path, magic)
        if not _looks_like_mp3(probe):
            raise StemArtifactError(
                f"{path.name} suffix is .mp3 but content is not MPEG audio"
            )
        return read_mp3_metadata(path), ".mp3"
    raise StemArtifactError(f"{path.name} uses unsupported container {suffix!r}")


def _read_mp3_probe(path: Path, initial: bytes) -> bytes:
    try:
        file_size = path.stat().st_size
        probe_len = min(file_size, 256 * 1024)
        if len(initial) >= probe_len:
            return initial[:probe_len]
        with path.open("rb") as source:
            return source.read(probe_len)
    except OSError:
        return initial


def _looks_like_mp3(data: bytes) -> bool:
    return _find_mp3_sync(data, _id3v2_skip_size(data)) >= 0


def read_flac_metadata(path: Path) -> WavMetadata:
    """Parse FLAC STREAMINFO without decoding audio."""
    try:
        with path.open("rb") as source:
            magic = source.read(4)
            if magic != b"fLaC":
                raise StemArtifactError(f"{path.name} is not a FLAC file (missing fLaC magic)")
            streaminfo: bytes | None = None
            while True:
                header = source.read(4)
                if len(header) != 4:
                    raise StemArtifactError(f"{path.name} has truncated FLAC metadata")
                block_header = struct.unpack(">I", header)[0]
                is_last = (block_header & 0x80000000) != 0
                block_type = (block_header & 0x7F000000) >> 24
                block_len = block_header & 0x00FFFFFF
                payload = source.read(block_len)
                if len(payload) != block_len:
                    raise StemArtifactError(f"{path.name} has truncated FLAC metadata block")
                if block_type == 0:
                    streaminfo = payload
                if is_last:
                    break
            if streaminfo is None:
                raise StemArtifactError(f"{path.name} FLAC missing STREAMINFO block")
            metadata = _parse_flac_streaminfo(path.name, streaminfo)
            _flac_tail_frame_covers_stream(path, streaminfo=streaminfo)
            return metadata
    except OSError as exc:
        raise StemArtifactError(
            f"cannot read FLAC metadata for {path.name}: {exc}"
        ) from exc


def _parse_flac_streaminfo(file_name: str, streaminfo: bytes) -> WavMetadata:
    if len(streaminfo) < 18:
        raise StemArtifactError(f"{file_name} STREAMINFO block is too short")
    packed = int.from_bytes(streaminfo[10:18], "big")
    sample_rate = (packed >> 44) & 0xFFFFF
    channels = ((packed >> 41) & 0x7) + 1
    frame_count = packed & 0xFFFFFFFFF
    if sample_rate <= 0 or channels <= 0 or frame_count <= 0:
        raise StemArtifactError(f"{file_name} FLAC STREAMINFO has invalid geometry")
    return WavMetadata(
        sample_rate=sample_rate,
        frame_count=frame_count,
        channels=channels,
    )


_FLAC_FIXED_BLOCK_SIZES: dict[int, int] = {
    1: 192,
    2: 576,
    3: 1152,
    4: 2304,
    5: 4608,
}


def _flac_crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _flac_read_utf8_number(data: bytes, offset: int) -> tuple[int, int] | None:
    if offset >= len(data):
        return None
    first = data[offset]
    if first & 0x80 == 0:
        return first, offset + 1
    if (first & 0xE0) == 0xC0:
        total_bytes = 2
        value = first & 0x1F
    elif (first & 0xF0) == 0xE0:
        total_bytes = 3
        value = first & 0x0F
    elif (first & 0xF8) == 0xF0:
        total_bytes = 4
        value = first & 0x07
    elif (first & 0xFC) == 0xF8:
        total_bytes = 5
        value = first & 0x03
    elif (first & 0xFE) == 0xFC:
        total_bytes = 6
        value = first & 0x01
    elif first == 0xFE:
        total_bytes = 7
        value = 0
    else:
        return None
    if offset + total_bytes > len(data):
        return None
    for index in range(1, total_bytes):
        follow = data[offset + index]
        if (follow & 0xC0) != 0x80:
            return None
        value = (value << 6) | (follow & 0x3F)
    return value, offset + total_bytes


def _flac_block_size_from_header(
    blocksize_code: int,
    data: bytes,
    offset: int,
    *,
    min_blocksize: int,
    max_blocksize: int,
) -> tuple[int, int] | None:
    del min_blocksize, max_blocksize
    if blocksize_code == 0:
        return None
    if blocksize_code in _FLAC_FIXED_BLOCK_SIZES:
        return _FLAC_FIXED_BLOCK_SIZES[blocksize_code], offset
    if blocksize_code == 6:
        if offset >= len(data):
            return None
        return data[offset] + 1, offset + 1
    if blocksize_code == 7:
        if offset + 2 > len(data):
            return None
        return struct.unpack(">H", data[offset : offset + 2])[0] + 1, offset + 2
    if 8 <= blocksize_code <= 15:
        return 256 << (blocksize_code - 8), offset
    return None


def _flac_valid_frame_header_at(  # noqa: PLR0911
    data: bytes,
    sync_offset: int,
    *,
    min_blocksize: int,
    max_blocksize: int,
) -> tuple[int, int, bool] | None:
    """Return (end_sample, header_end_offset, variable_blocksize) if valid."""
    if sync_offset + 4 > len(data):
        return None
    if data[sync_offset] != 0xFF or data[sync_offset + 1] not in {0xF8, 0xF9}:
        return None
    variable_blocksize = (data[sync_offset + 1] & 0x01) != 0
    blocksize_code = (data[sync_offset + 2] >> 4) & 0x0F
    if data[sync_offset + 3] & 0x01:
        return None
    header_start = sync_offset
    offset = sync_offset + 4
    parsed = _flac_read_utf8_number(data, offset)
    if parsed is None:
        return None
    number, offset = parsed
    block_parsed = _flac_block_size_from_header(
        blocksize_code,
        data,
        offset,
        min_blocksize=min_blocksize,
        max_blocksize=max_blocksize,
    )
    if block_parsed is None:
        return None
    block_size, offset = block_parsed
    if block_size <= 0:
        return None
    if offset >= len(data):
        return None
    header_bytes = data[header_start:offset]
    if _flac_crc8(header_bytes) != data[offset]:
        return None
    end_sample = (
        number + block_size
        if variable_blocksize
        else number * max_blocksize + block_size
    )
    return end_sample, offset + 1, variable_blocksize


def _flac_tail_frame_covers_stream(path: Path, *, streaminfo: bytes) -> None:
    """Require the file tail contains a genuine frame reaching STREAMINFO total_samples."""
    if len(streaminfo) < 18:
        raise StemArtifactError(f"{path.name} STREAMINFO block is too short")
    min_blocksize = struct.unpack(">H", streaminfo[0:2])[0]
    max_blocksize = struct.unpack(">H", streaminfo[2:4])[0]
    packed = int.from_bytes(streaminfo[10:18], "big")
    total_samples = packed & 0xFFFFFFFFF
    if min_blocksize <= 0 or max_blocksize <= 0 or total_samples <= 0:
        raise StemArtifactError(f"{path.name} FLAC STREAMINFO has invalid geometry")
    try:
        file_size = path.stat().st_size
        tail_len = min(file_size, 65_536)
        with path.open("rb") as source:
            source.seek(file_size - tail_len)
            tail = source.read(tail_len)
    except OSError as exc:
        raise StemArtifactError(
            f"cannot read FLAC tail for {path.name}: {exc}"
        ) from exc
    # A candidate's end_sample is computed from its OWN decoded block size, so a
    # genuinely final frame always ends at exactly total_samples - no slack is
    # needed or safe here. A "within one block" tolerance would always accept the
    # SECOND-to-last frame too (its end_sample is, by construction, never more than
    # one block short of total_samples), which would silently accept a file
    # truncated right after that frame while the true last frame is missing. Only
    # exact equality closes that gap.
    best_end: int | None = None
    found_exact = False
    for index in range(len(tail) - 1):
        if tail[index] != 0xFF or tail[index + 1] not in {0xF8, 0xF9}:
            continue
        validated = _flac_valid_frame_header_at(
            tail,
            index,
            min_blocksize=min_blocksize,
            max_blocksize=max_blocksize,
        )
        if validated is None:
            continue
        best_end = validated[0]
        if best_end == total_samples:
            found_exact = True
            break
    if not found_exact:
        found = "no valid frame header found in file tail"
        if best_end is not None:
            found = f"last tail frame candidate ends at sample {best_end}"
        raise StemArtifactError(
            f"{path.name} FLAC tail frame check failed: STREAMINFO declares "
            f"{total_samples} total samples but {found} "
            f"(need a frame whose own decoded end sample equals {total_samples} "
            "exactly)"
        )


def read_mp3_metadata(path: Path) -> WavMetadata:
    """Parse MP3 geometry from frame headers without decoding or subprocess."""
    try:
        file_size = path.stat().st_size
        data = path.read_bytes()
    except OSError as exc:
        raise StemArtifactError(
            f"cannot read MP3 metadata for {path.name}: {exc}"
        ) from exc
    if file_size < 4:
        raise StemArtifactError(f"{path.name} is too short to be MP3")

    id3v2_size = _id3v2_skip_size(data)
    sync = _find_mp3_sync(data, id3v2_size)
    if sync < 0 or sync + 4 > len(data):
        raise StemArtifactError(f"{path.name} has no MPEG audio sync word")

    header = data[sync : sync + 4]
    version_id, layer, bitrate_idx, sample_rate_idx, padding, channels = (
        _parse_mpeg_frame_header(header)
    )
    if layer != 3:
        raise StemArtifactError(f"{path.name} is not MPEG Layer III")

    sample_rate = _mpeg_sample_rate(version_id, sample_rate_idx)
    bitrate_kbps = _mpeg_bitrate_kbps(version_id, layer, bitrate_idx)
    if sample_rate <= 0 or channels <= 0:
        raise StemArtifactError(f"{path.name} has invalid MP3 header geometry")
    if bitrate_kbps <= 0:
        raise StemArtifactError(f"{path.name} uses free-format or invalid MP3 bitrate")

    samples_per_frame = 1152 if version_id == 3 else 576
    frame_size = _mpeg_layer3_frame_size(
        version_id, bitrate_kbps, sample_rate, padding
    )
    if frame_size <= 0 or sync + frame_size > len(data):
        raise StemArtifactError(f"{path.name} has truncated MP3 frame")

    frame_count = _mp3_frame_count(
        data,
        sync=sync,
        version_id=version_id,
        channels=channels,
        samples_per_frame=samples_per_frame,
        file_size=file_size,
        bitrate_kbps=bitrate_kbps,
        sample_rate=sample_rate,
        file_name=path.name,
        id3v2_size=id3v2_size,
    )
    if frame_count <= 0:
        raise StemArtifactError(f"{path.name} has zero MP3 frames")
    return WavMetadata(
        sample_rate=sample_rate,
        frame_count=frame_count,
        channels=channels,
    )


def _id3v2_skip_size(data: bytes) -> int:
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    size = (
        ((data[6] & 0x7F) << 21)
        | ((data[7] & 0x7F) << 14)
        | ((data[8] & 0x7F) << 7)
        | (data[9] & 0x7F)
    )
    return 10 + size


def _find_mp3_sync(data: bytes, start: int) -> int:
    limit = len(data) - 1
    for index in range(start, limit):
        if data[index] == 0xFF and (data[index + 1] & 0xE0) == 0xE0:
            return index
    return -1


def _parse_mpeg_frame_header(header: bytes) -> tuple[int, int, int, int, int, int]:
    if len(header) != 4 or header[0] != 0xFF or (header[1] & 0xE0) != 0xE0:
        raise StemArtifactError("invalid MPEG frame header")
    version_id = (header[1] >> 3) & 0x03
    layer = 4 - ((header[1] >> 1) & 0x03)
    bitrate_idx = (header[2] >> 4) & 0x0F
    sample_rate_idx = (header[2] >> 2) & 0x03
    padding = (header[2] >> 1) & 0x01
    channel_mode = (header[3] >> 6) & 0x03
    mode_extension = (header[3] >> 4) & 0x03
    channels = _mpeg_channels(channel_mode, mode_extension)
    return version_id, layer, bitrate_idx, sample_rate_idx, padding, channels


def _mpeg_channels(channel_mode: int, mode_extension: int) -> int:
    del mode_extension
    if channel_mode == 3:
        return 1
    return 2


def _mpeg_sample_rate(version_id: int, sample_rate_idx: int) -> int:
    if sample_rate_idx == 3:
        return 0
    try:
        return _MPEG_SAMPLE_RATES[version_id][sample_rate_idx]
    except (KeyError, IndexError):
        return 0


def _mpeg_bitrate_kbps(version_id: int, layer: int, bitrate_idx: int) -> int:
    if bitrate_idx in {0, 15}:
        return 0
    if layer != 3:
        return 0
    table = _MPEG1_L3_BITRATES_KBPS if version_id == 3 else _MPEG2_L3_BITRATES_KBPS
    return table[bitrate_idx]


def _mpeg_layer3_frame_size(
    version_id: int, bitrate_kbps: int, sample_rate: int, padding: int
) -> int:
    if sample_rate <= 0 or bitrate_kbps <= 0:
        return 0
    if version_id == 3:
        return (144_000 * bitrate_kbps) // sample_rate + padding
    return (72_000 * bitrate_kbps) // sample_rate + padding


def _mp3_side_info_len(version_id: int, channels: int) -> int:
    if version_id == 3:
        return 17 if channels == 1 else 32
    return 9 if channels == 1 else 17


def _validate_mp3_xing_length(  # noqa: PLR0913
    file_name: str,
    *,
    data: bytes,
    file_size: int,
    id3v2_size: int,
    xing_offset: int,
    flags: int,
    frames: int,
    version_id: int,
    bitrate_kbps: int,
    sample_rate: int,
) -> None:
    audio_bytes = file_size - id3v2_size
    field_offset = xing_offset + 8
    if flags & 0x01:
        field_offset += 4
    if flags & 0x02:
        if field_offset + 4 > len(data):
            raise StemArtifactError(
                f"{file_name} MP3 Xing/Info header declares bytes but is truncated"
            )
        xing_bytes = struct.unpack(">I", data[field_offset : field_offset + 4])[0]
        if audio_bytes < xing_bytes:
            raise StemArtifactError(
                f"{file_name} MP3 declared audio length {xing_bytes} bytes "
                f"(id3v2-adjusted) but on-disk audio is {audio_bytes} bytes"
            )
        return
    if flags & 0x01:
        min_frame_bytes = _mpeg_layer3_frame_size(
            version_id, bitrate_kbps, sample_rate, padding=0
        )
        expected_min = int(frames * min_frame_bytes * 0.9)
        if audio_bytes < expected_min:
            raise StemArtifactError(
                f"{file_name} MP3 on-disk audio is {audio_bytes} bytes but "
                f"Xing/Info frames field implies at least {expected_min} bytes"
            )


def _mp3_trailing_data_allowed(data: bytes, offset: int) -> bool:
    remaining = len(data) - offset
    return (
        remaining == 0
        or (remaining == 128 and data[offset : offset + 3] == b"TAG")
        or (remaining >= 32 and data[-32:-24] == b"APETAGEX")
    )


def scan_mp3_frames(  # noqa: C901
    data: bytes,
    *,
    sync: int,
    file_name: str,
    expected_version_id: int,
    expected_layer: int = 3,
) -> int:
    """Walk MPEG Layer III frame headers from sync to EOF and count valid frames."""
    offset = sync
    frame_count = 0
    expected_sample_rate: int | None = None
    while offset + 4 <= len(data):
        if data[offset] != 0xFF or (data[offset + 1] & 0xE0) != 0xE0:
            if _mp3_trailing_data_allowed(data, offset):
                break
            raise StemArtifactError(
                f"{file_name} invalid MP3 frame sync at byte offset {offset}"
            )
        try:
            version_id, layer, bitrate_idx, sample_rate_idx, padding, _channels = (
                _parse_mpeg_frame_header(data[offset : offset + 4])
            )
        except StemArtifactError as exc:
            if _mp3_trailing_data_allowed(data, offset):
                break
            raise StemArtifactError(
                f"{file_name} invalid MP3 frame header at byte offset {offset}"
            ) from exc
        if layer != expected_layer:
            raise StemArtifactError(
                f"{file_name} MP3 layer changed at byte offset {offset}"
            )
        if version_id != expected_version_id:
            raise StemArtifactError(
                f"{file_name} MP3 version changed at byte offset {offset}"
            )
        sample_rate = _mpeg_sample_rate(version_id, sample_rate_idx)
        bitrate_kbps = _mpeg_bitrate_kbps(version_id, layer, bitrate_idx)
        if sample_rate <= 0 or bitrate_kbps <= 0:
            raise StemArtifactError(
                f"{file_name} invalid MP3 frame geometry at byte offset {offset}"
            )
        if expected_sample_rate is None:
            expected_sample_rate = sample_rate
        elif sample_rate != expected_sample_rate:
            raise StemArtifactError(
                f"{file_name} MP3 sample rate changed at byte offset {offset}"
            )
        frame_size = _mpeg_layer3_frame_size(
            version_id, bitrate_kbps, sample_rate, padding
        )
        if frame_size <= 0 or offset + frame_size > len(data):
            raise StemArtifactError(
                f"{file_name} truncated MP3 frame at byte offset {offset}"
            )
        frame_count += 1
        offset += frame_size
    if frame_count == 0:
        raise StemArtifactError(f"{file_name} has zero MP3 frames")
    if offset < len(data) and not _mp3_trailing_data_allowed(data, offset):
        raise StemArtifactError(
            f"{file_name} unexpected trailing data at byte offset {offset}"
        )
    return frame_count


def _mp3_frame_count(  # noqa: PLR0913
    data: bytes,
    *,
    sync: int,
    version_id: int,
    channels: int,
    samples_per_frame: int,
    file_size: int,
    bitrate_kbps: int,
    sample_rate: int,
    file_name: str,
    id3v2_size: int,
) -> int:
    side_info_len = _mp3_side_info_len(version_id, channels)
    xing_offset = sync + 4 + side_info_len
    if xing_offset + 8 <= len(data):
        tag = data[xing_offset : xing_offset + 4]
        if tag in {b"Xing", b"Info"}:
            flags = struct.unpack(">I", data[xing_offset + 4 : xing_offset + 8])[0]
            if flags & 0x01:
                if xing_offset + 12 > len(data):
                    raise StemArtifactError(
                        f"{file_name} MP3 Xing/Info header is truncated"
                    )
                frames = struct.unpack(">I", data[xing_offset + 8 : xing_offset + 12])[0]
                if frames > 0:
                    _validate_mp3_xing_length(
                        file_name,
                        data=data,
                        file_size=file_size,
                        id3v2_size=id3v2_size,
                        xing_offset=xing_offset,
                        flags=flags,
                        frames=frames,
                        version_id=version_id,
                        bitrate_kbps=bitrate_kbps,
                        sample_rate=sample_rate,
                    )
                    return frames * samples_per_frame
    scanned = scan_mp3_frames(
        data,
        sync=sync,
        file_name=file_name,
        expected_version_id=version_id,
    )
    return scanned * samples_per_frame


def _align_v1_part_metadata(
    reference: WavMetadata,
    candidate: WavMetadata,
    *,
    reference_part: str,
    part: str,
    container: str,
) -> WavMetadata:
    if candidate.sample_rate != reference.sample_rate:
        raise StemArtifactError(
            f"{part} sample_rate {candidate.sample_rate} does not match "
            f"{reference_part} sample_rate {reference.sample_rate}"
        )
    if candidate.channels != reference.channels:
        raise StemArtifactError(
            f"{part} channels {candidate.channels} does not match "
            f"{reference_part} channels {reference.channels}"
        )
    if container == ".wav":
        if candidate.frame_count != reference.frame_count:
            raise StemArtifactError(
                f"{part} metadata {candidate!r} does not align with "
                f"{reference_part} metadata {reference!r}"
            )
        return reference
    if container == ".flac":
        if candidate.frame_count != reference.frame_count:
            raise StemArtifactError(
                f"{part} metadata {candidate!r} does not align with "
                f"{reference_part} metadata {reference!r}"
            )
        return reference
    frame_gap = abs(candidate.frame_count - reference.frame_count)
    if frame_gap > FRAME_MISMATCH_TOL_S * reference.sample_rate:
        raise StemArtifactError(
            f"{part} frame_count {candidate.frame_count} differs from "
            f"{reference_part} frame_count {reference.frame_count} by {frame_gap} "
            f"frames (> {FRAME_MISMATCH_TOL_S}s at {reference.sample_rate} Hz)"
        )
    min_frames = min(reference.frame_count, candidate.frame_count)
    return WavMetadata(
        sample_rate=reference.sample_rate,
        frame_count=min_frames,
        channels=reference.channels,
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


def summarize_stem_bundle(
    stable_id: str, *, stems_dir: Path = DEFAULT_STEMS_DIR
) -> dict[str, Any]:
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


def bulk_stem_summaries(
    stable_ids: Iterable[str], *, stems_dir: Path = DEFAULT_STEMS_DIR
) -> dict[str, dict[str, Any]]:
    """``summarize_stem_bundle`` for a listing page, keyed by stable_id.

    Repeat pages and the 60 s library re-poll are served from that function's
    TTL cache, so a warm page touches the filesystem not at all.
    """
    return {sid: summarize_stem_bundle(sid, stems_dir=stems_dir) for sid in stable_ids}


__all__ = [
    "DEFAULT_STEMS_DIR",
    "ROFORMER_PARTS",
    "ROFORMER_STEMS_DIR",
    "STEM_LAYOUTS",
    "STEM_PARTS",
    "STEM_SUMMARY_TTL_S",
    "DemucsModel",
    "StemArtifactError",
    "StemBundle",
    "StemBundleNotFoundError",
    "StemManifest",
    "StemPart",
    "StemSourceProvenance",
    "WavMetadata",
    "bulk_stem_summaries",
    "load_stem_bundle",
    "read_flac_metadata",
    "read_mp3_metadata",
    "read_stem_container_metadata",
    "read_wav_metadata",
    "scan_mp3_frames",
    "stem_roots",
    "summarize_stem_bundle",
    "validate_stable_id",
]
