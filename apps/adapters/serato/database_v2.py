"""Serato ``database V2`` crate-DB codec.

Reads + writes the top-level Serato library database file. This is a
tag-stream file (reuse ``apps.adapters.serato.tagstream.TagStream``) where
each top-level ``otrk`` tag is one track row.

Within an ``otrk`` container, per-track fields appear as leaf tags:

  * ``pfil`` -- file path (UTF-16-BE string)
  * ``tsng`` -- title (UTF-16-BE)
  * ``tart`` -- artist (UTF-16-BE)
  * ``talb`` -- album (UTF-16-BE)
  * ``tbpm`` -- BPM (UTF-16-BE decimal string)
  * ``tkey`` -- key (UTF-16-BE, Serato native format)
  * ``ttyp`` -- file type (UTF-16-BE: ``"mp3"``/``"flac"``/...)

Every string is UTF-16-BE (Mixxx ``seratofeature.cpp`` decodes them all that
way). Older builds of this codec wrote ``tbpm`` and ``ttyp`` as ASCII; the
reader still accepts those.
  * ``tcmp`` -- composer
  * ``tgrp`` -- grouping

Unknown leaf tags are preserved per open-dj §9.

The DB also carries top-level metadata tags (``vrsn`` -- version string,
``sbav`` -- sort/view state). We round-trip those verbatim without
interpreting them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.adapters.serato.tagstream import (
    ContainerTag,
    RawTag,
    Tag,
    TagStream,
    TagStreamError,
)

# ---------------------------------------------------------------- encoding


def _decode_utf16be(data: bytes) -> str:
    # Serato string payloads are UTF-16 big-endian, not null-terminated.
    return data.decode("utf-16-be", errors="replace")


def _encode_utf16be(text: str) -> bytes:
    return text.encode("utf-16-be")


def _decode_legacy_ascii_or_utf16be(data: bytes) -> str:
    """For ``tbpm`` and ``ttyp`` only: UTF-16-BE, as Serato writes them, or
    the ASCII this codec used to write. Their text is digits, a dot or a short
    lowercase extension, so as UTF-16-BE every other byte is NUL; a payload
    with no NUL at all is the legacy ASCII. (Not safe for titles: CJK text in
    UTF-16-BE can have no NUL bytes.)"""
    if data and b"\x00" not in data:
        return data.decode("ascii", errors="replace")
    return _decode_utf16be(data)


# --------------------------------------------------------------- dataclass


@dataclass
class CrateTrack:
    """One Serato ``otrk`` record mapped to typed fields.

    ``extra_tags`` holds any leaf tag we did not explicitly decode so that
    ``CrateTrack -> bytes -> CrateTrack`` is a round-trip no-op. This is the
    open-dj §9 "preserve unknowns" contract.
    """

    file_path: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    bpm: str = ""        # kept as the decimal string Serato writes
    key: str = ""
    file_type: str = ""
    composer: str = ""
    grouping: str = ""
    extra_tags: tuple[RawTag, ...] = field(default_factory=tuple)

    _KNOWN: tuple[tuple[str, str, str], ...] = (
        # (tag_type, attribute, codec); every Serato string is UTF-16-BE
        ("pfil", "file_path", "utf16be"),
        ("tsng", "title", "utf16be"),
        ("tart", "artist", "utf16be"),
        ("talb", "album", "utf16be"),
        ("tbpm", "bpm", "utf16be_or_legacy_ascii"),
        ("tkey", "key", "utf16be"),
        ("ttyp", "file_type", "utf16be_or_legacy_ascii"),
        ("tcmp", "composer", "utf16be"),
        ("tgrp", "grouping", "utf16be"),
    )

    # ---------------------------------------------------------- to/from tags

    @classmethod
    def from_tag(cls, container: ContainerTag) -> "CrateTrack":
        assert container.type == "otrk", f"expected otrk, got {container.type!r}"
        known_by_type = {tt: (attr, codec) for tt, attr, codec in cls._KNOWN}
        values: dict[str, Any] = {}
        extras: list[RawTag] = []
        for child in container.children:
            if isinstance(child, RawTag) and child.type in known_by_type:
                attr, codec = known_by_type[child.type]
                values[attr] = (
                    _decode_legacy_ascii_or_utf16be(child.payload)
                    if codec == "utf16be_or_legacy_ascii"
                    else _decode_utf16be(child.payload)
                )
            elif isinstance(child, RawTag):
                extras.append(child)
            else:
                # Nested container inside an otrk -- rare; treat as opaque raw
                # by re-encoding it.
                extras.append(
                    RawTag(type=child.type, payload=TagStream.write((child,)))
                )
        return cls(extra_tags=tuple(extras), **values)

    def to_tag(self) -> ContainerTag:
        children: list[Tag] = []
        for tag_type, attr, _codec in self._KNOWN:
            value = getattr(self, attr)
            if not value:
                # Skip empty strings -- Serato itself omits zero-length tags.
                continue
            payload = _encode_utf16be(value)
            children.append(RawTag(type=tag_type, payload=payload))
        children.extend(self.extra_tags)
        return ContainerTag(type="otrk", children=tuple(children))


# --------------------------------------------------------------- container


@dataclass
class DatabaseV2:
    """Decoded view of a ``database V2`` file.

    ``tracks`` are the ``otrk`` entries in order; ``header_tags`` holds every
    non-``otrk`` top-level tag (version, sort state, column descriptors) in
    file order so re-encoding is byte-stable.
    """

    tracks: tuple[CrateTrack, ...] = ()
    header_tags: tuple[Tag, ...] = ()
    trailer_tags: tuple[Tag, ...] = ()

    # ----------------------------------------------------------- read/write

    @classmethod
    def read(cls, source: Path | bytes) -> "DatabaseV2":
        if isinstance(source, (str, Path)):
            data = Path(source).read_bytes()
        else:
            data = bytes(source)
        try:
            tags = TagStream.read(data)
        except TagStreamError as exc:
            raise ValueError(f"invalid database V2: {exc}") from exc
        header: list[Tag] = []
        tracks: list[CrateTrack] = []
        trailer: list[Tag] = []
        state = "header"
        for tag in tags:
            if isinstance(tag, ContainerTag) and tag.type == "otrk":
                state = "tracks"
                tracks.append(CrateTrack.from_tag(tag))
            elif state == "header":
                header.append(tag)
            else:
                trailer.append(tag)
        return cls(
            tracks=tuple(tracks),
            header_tags=tuple(header),
            trailer_tags=tuple(trailer),
        )

    def to_bytes(self) -> bytes:
        tags: list[Tag] = list(self.header_tags)
        tags.extend(t.to_tag() for t in self.tracks)
        tags.extend(self.trailer_tags)
        return TagStream.write(tags)

    def write(self, target: Path) -> None:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.to_bytes())


# ------------------------------------------------------------- subcrate


@dataclass
class Subcrate:
    """A per-crate playlist file ``Subcrates/<name>.crate``.

    Each track is an ``otrk`` container holding a ``ptrk`` tag, the UTF-16-BE
    path of the track (Mixxx ``seratofeature.cpp`` ``parseCrateTrackPath``).
    A bare top-level ``ptrk``, which older builds of this codec wrote, is read
    too. Other tags (column configs, sort modes) are preserved on
    ``header_tags`` for byte-stable round-trip.
    """

    name: str
    track_paths: tuple[str, ...] = ()
    header_tags: tuple[Tag, ...] = ()
    trailer_tags: tuple[Tag, ...] = ()

    @classmethod
    def read(cls, source: Path) -> "Subcrate":
        data = Path(source).read_bytes()
        tags = TagStream.read(data)
        name = Path(source).stem
        paths: list[str] = []
        header: list[Tag] = []
        trailer: list[Tag] = []
        state = "header"
        for tag in tags:
            path = _crate_track_path(tag)
            if path is not None:
                state = "tracks"
                paths.append(path)
            elif state == "header":
                header.append(tag)
            else:
                trailer.append(tag)
        return cls(
            name=name,
            track_paths=tuple(paths),
            header_tags=tuple(header),
            trailer_tags=tuple(trailer),
        )

    def to_bytes(self) -> bytes:
        tags: list[Tag] = list(self.header_tags)
        tags.extend(
            ContainerTag(type="otrk", children=(RawTag(type="ptrk", payload=_encode_utf16be(p)),))
            for p in self.track_paths
        )
        tags.extend(self.trailer_tags)
        return TagStream.write(tags)

    def write(self, target: Path) -> None:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.to_bytes())


def _crate_track_path(tag: Tag) -> str | None:
    """The track path a crate tag names, or None for a non-track tag."""
    if isinstance(tag, RawTag) and tag.type == "ptrk":
        return _decode_utf16be(tag.payload)
    if isinstance(tag, ContainerTag) and tag.type == "otrk":
        for child in tag.children:
            if isinstance(child, RawTag) and child.type == "ptrk":
                return _decode_utf16be(child.payload)
        raise ValueError("crate otrk entry has no ptrk path")
    return None
