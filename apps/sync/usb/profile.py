"""USB profile YAML loader + validation.

Profile schema (locked in ``10-CONTEXT.md#D1``, elaborated in
``10-RESEARCH.md#8``)::

    name: gigA
    drive_label: GIG-A
    drive_uuid: AABBCCDD-1122-33...       # optional; first run writes it
    playlists:
      - "Afro House 2026"
    format: copy-as-is                    # or "mp3@320"
    layout: "Artist/Album/Track"          # or "Playlist/NN - Artist - Track" or "flat"
    playlist_files: m3u8                  # or "none"
    conflict_policy: canonical-wins       # or "skip" or "backup-then-overwrite"
    exclusions:                           # optional blacklist (rel paths)
      - "01_samples/"
    export_opendj: false                  # gated; Phase 15 enables

No third-party validation library is used -- ``pydantic`` is not yet in
``requirements.txt`` and pulling it in for one dataclass is wasteful.
The loader does its own strict checking: unknown keys fail loudly,
enums are validated, ``playlists`` must be non-empty.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Enums (kept as literal sets -- simpler to grep than Enum classes here).
FORMATS: frozenset[str] = frozenset({"copy-as-is", "mp3@320"})
LAYOUTS: frozenset[str] = frozenset(
    {"Artist/Album/Track", "Playlist/NN - Artist - Track", "flat"}
)
PLAYLIST_FILES: frozenset[str] = frozenset({"m3u8", "none"})
CONFLICT_POLICIES: frozenset[str] = frozenset(
    {"canonical-wins", "skip", "backup-then-overwrite"}
)

_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "drive_label",
        "drive_uuid",
        "playlists",
        "format",
        "layout",
        "playlist_files",
        "conflict_policy",
        "exclusions",
        "export_opendj",
    }
)
_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "drive_label",
        "playlists",
        "format",
        "layout",
        "playlist_files",
        "conflict_policy",
    }
)


class ProfileError(ValueError):
    """Raised when a profile YAML fails schema validation."""


@dataclass(slots=True, frozen=True)
class Profile:
    """Validated USB profile."""

    name: str
    drive_label: str
    playlists: tuple[str, ...]
    format: str
    layout: str
    playlist_files: str
    conflict_policy: str
    drive_uuid: str | None = None
    exclusions: tuple[str, ...] = field(default_factory=tuple)
    export_opendj: bool = False

    @property
    def mount_point(self) -> Path:
        """Standard macOS mount point derived from ``drive_label``."""
        return Path("/Volumes") / self.drive_label

    @property
    def needs_transcode(self) -> bool:
        return self.format == "mp3@320"


def _require(key: str, value: Any, pred: Any, hint: str) -> None:
    """Raise :class:`ProfileError` if ``pred`` is false."""
    if not pred:
        raise ProfileError(f"profile.{key}: {hint} (got {value!r})")


def _validate(raw: dict[str, Any]) -> Profile:
    if not isinstance(raw, dict):
        raise ProfileError("profile root must be a mapping")

    unknown = set(raw) - _ALLOWED_KEYS
    if unknown:
        raise ProfileError(
            f"unknown keys in profile: {sorted(unknown)} "
            f"(allowed: {sorted(_ALLOWED_KEYS)})"
        )

    missing = _REQUIRED_KEYS - set(raw)
    if missing:
        raise ProfileError(f"missing required keys: {sorted(missing)}")

    name = raw["name"]
    _require("name", name, isinstance(name, str) and name.strip(), "must be non-empty string")

    drive_label = raw["drive_label"]
    _require(
        "drive_label",
        drive_label,
        isinstance(drive_label, str) and drive_label.strip(),
        "must be non-empty string",
    )

    playlists_raw = raw["playlists"]
    _require(
        "playlists",
        playlists_raw,
        isinstance(playlists_raw, list) and len(playlists_raw) > 0,
        "must be a non-empty list of playlist names",
    )
    playlists: list[str] = []
    for item in playlists_raw:
        _require("playlists[]", item, isinstance(item, str) and item.strip(), "each item must be a non-empty string")
        playlists.append(item.strip())

    format_ = raw["format"]
    _require("format", format_, format_ in FORMATS, f"must be one of {sorted(FORMATS)}")
    layout = raw["layout"]
    _require("layout", layout, layout in LAYOUTS, f"must be one of {sorted(LAYOUTS)}")
    playlist_files = raw["playlist_files"]
    _require(
        "playlist_files",
        playlist_files,
        playlist_files in PLAYLIST_FILES,
        f"must be one of {sorted(PLAYLIST_FILES)}",
    )
    conflict_policy = raw["conflict_policy"]
    _require(
        "conflict_policy",
        conflict_policy,
        conflict_policy in CONFLICT_POLICIES,
        f"must be one of {sorted(CONFLICT_POLICIES)}",
    )

    drive_uuid = raw.get("drive_uuid")
    if drive_uuid is not None:
        _require("drive_uuid", drive_uuid, isinstance(drive_uuid, str), "must be a string if set")

    exclusions_raw = raw.get("exclusions", []) or []
    _require("exclusions", exclusions_raw, isinstance(exclusions_raw, list), "must be a list if set")
    exclusions: list[str] = []
    for item in exclusions_raw:
        _require("exclusions[]", item, isinstance(item, str), "each item must be a string")
        exclusions.append(item)

    export_opendj = bool(raw.get("export_opendj", False))

    return Profile(
        name=name.strip(),
        drive_label=drive_label.strip(),
        playlists=tuple(playlists),
        format=format_,
        layout=layout,
        playlist_files=playlist_files,
        conflict_policy=conflict_policy,
        drive_uuid=drive_uuid,
        exclusions=tuple(exclusions),
        export_opendj=export_opendj,
    )


def load(path: Path | str) -> Profile:
    """Load a profile from disk, validating strictly.

    Raises :class:`ProfileError` on any schema violation; propagates
    :class:`FileNotFoundError` and ``yaml.YAMLError`` unchanged so
    callers can distinguish file-system vs parse vs validation errors.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    return _validate(raw)


def load_from_string(text: str) -> Profile:
    """Convenience for tests."""
    return _validate(yaml.safe_load(text))


__all__ = [
    "Profile",
    "ProfileError",
    "FORMATS",
    "LAYOUTS",
    "PLAYLIST_FILES",
    "CONFLICT_POLICIES",
    "load",
    "load_from_string",
]
