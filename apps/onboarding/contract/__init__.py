"""Public surface of ``apps.onboarding`` (LIBRARY-HYGIENE-13506, issue #12247).

Code outside ``apps.onboarding`` imports only this module. Delivery layers and
build scripts use it to verify bundled demo media against
``demo-tracks.manifest.json``. Stdlib-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CONTRACT_VERSION = "onboarding/1"

_ONBOARDING_DIR = Path(__file__).resolve().parent.parent
BUNDLE_ROOT = _ONBOARDING_DIR / "demo-tracks"
MANIFEST_PATH = _ONBOARDING_DIR / "demo-tracks.manifest.json"
_MANIFEST_NAME = "demo-tracks.manifest.json"

AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".flac", ".m4a", ".aiff", ".aif", ".ogg", ".opus"})

_REQUIRED_LICENCE_FIELDS = ("licence_source", "attribution")


@dataclass(frozen=True, slots=True)
class DemoTrackEntry:
    id: str
    media: str
    licence_source: str
    attribution: str


@dataclass(frozen=True, slots=True)
class DemoTracksManifest:
    schema_version: int
    tracks: tuple[DemoTrackEntry, ...]


class DemoTrackLicenceError(Exception):
    """Raised when bundled demo media fails the licence manifest check."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def _posix_relative(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    return rel.as_posix()


def bundled_media_paths(bundle_root: Path) -> list[str]:
    """Every audio file under ``bundle_root``, as repo-relative POSIX paths from that root."""
    if not bundle_root.is_dir():
        return []
    paths: list[str] = []
    for file in sorted(bundle_root.rglob("*")):
        if not file.is_file():
            continue
        if file.name == _MANIFEST_NAME:
            continue
        if file.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        paths.append(_posix_relative(file, bundle_root))
    return paths


def load_manifest(manifest_path: Path) -> DemoTracksManifest:
    import json

    with manifest_path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise DemoTrackLicenceError(f"{manifest_path}: manifest root must be an object")
    schema_version = raw.get("schema_version")
    if schema_version != 1:
        raise DemoTrackLicenceError(f"{manifest_path}: unsupported schema_version {schema_version!r} (expected 1)")
    tracks_raw = raw.get("tracks")
    if tracks_raw is None:
        raise DemoTrackLicenceError(f"{manifest_path}: missing tracks list")
    if not isinstance(tracks_raw, list):
        raise DemoTrackLicenceError(f"{manifest_path}: tracks must be a list")
    entries: list[DemoTrackEntry] = []
    seen_media: set[str] = set()
    seen_ids: set[str] = set()
    for index, item in enumerate(tracks_raw):
        label = f"{manifest_path}: tracks[{index}]"
        if not isinstance(item, dict):
            raise DemoTrackLicenceError(f"{label} must be an object")
        track_id = item.get("id")
        media = item.get("media")
        if not isinstance(track_id, str) or not track_id.strip():
            raise DemoTrackLicenceError(f"{label}: id must be a non-empty string")
        if not isinstance(media, str) or not media.strip():
            raise DemoTrackLicenceError(f"{label}: media must be a non-empty string")
        media_norm = media.replace("\\", "/").lstrip("./")
        if ".." in media_norm.split("/"):
            raise DemoTrackLicenceError(f"{label}: media must stay inside the bundle ({media!r})")
        if track_id in seen_ids:
            raise DemoTrackLicenceError(f"{manifest_path}: duplicate track id {track_id!r}")
        if media_norm in seen_media:
            raise DemoTrackLicenceError(f"{manifest_path}: duplicate media path {media_norm!r}")
        seen_ids.add(track_id)
        seen_media.add(media_norm)
        licence_source = item.get("licence_source")
        attribution = item.get("attribution")
        missing = [
            name
            for name, value in (
                ("licence_source", licence_source),
                ("attribution", attribution),
            )
            if not isinstance(value, str) or not value.strip()
        ]
        if missing:
            raise DemoTrackLicenceError(f"{label}: missing or empty {', '.join(missing)}")
        entries.append(
            DemoTrackEntry(
                id=track_id,
                media=media_norm,
                licence_source=str(licence_source).strip(),
                attribution=str(attribution).strip(),
            )
        )
    return DemoTracksManifest(schema_version=1, tracks=tuple(entries))


def verify_demo_track_licences(
    *,
    bundle_root: Path = BUNDLE_ROOT,
    manifest_path: Path = MANIFEST_PATH,
) -> list[str]:
    """Return human-readable errors; an empty list means the bundle is complete."""
    errors: list[str] = []
    try:
        manifest = load_manifest(manifest_path)
    except DemoTrackLicenceError as exc:
        return [str(exc)]

    media_on_disk = set(bundled_media_paths(bundle_root))
    media_in_manifest = {entry.media for entry in manifest.tracks}

    for rel in sorted(media_on_disk - media_in_manifest):
        errors.append(f"bundled demo track {rel!r} has no manifest entry")

    for entry in manifest.tracks:
        target = bundle_root / Path(*entry.media.split("/"))
        if not target.is_file():
            errors.append(f"manifest entry {entry.id!r} references missing media {entry.media!r}")
            continue
        for field in _REQUIRED_LICENCE_FIELDS:
            value = getattr(entry, field)
            if not value.strip():
                errors.append(f"manifest entry {entry.id!r} lacks {field}")

    return errors


__all__ = [
    "AUDIO_SUFFIXES",
    "BUNDLE_ROOT",
    "CONTRACT_VERSION",
    "MANIFEST_PATH",
    "DemoTrackEntry",
    "DemoTrackLicenceError",
    "DemoTracksManifest",
    "bundled_media_paths",
    "load_manifest",
    "verify_demo_track_licences",
]
