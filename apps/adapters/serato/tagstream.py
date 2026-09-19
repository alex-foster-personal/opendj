"""Serato tag-stream codec -- clean-room implementation.

Serato uses the same tag-stream layout in several places: the ``database V2``
crate DB, the per-file ``Serato Markers2`` GEOB ID3 frame, per-crate files.
The layout is straightforward:

    tag := type (4 bytes ASCII) + length (uint32 big-endian) + payload
    payload := bytes (opaque) or nested tag stream

Container tag types (those whose payload is itself a tag stream) are listed
in ``CONTAINER_TYPES``. Any unknown type is preserved as an opaque
``RawTag(type, payload)`` so adapters round-trip unknown data per open-dj §9.

References (docs only, no source copied):

  * triseratops README (MPL-2): describes the 4+4 tag framing.
  * serato-tags Kaitai schemas (CC-BY-SA-4.0): enumerate container types.

This module is self-contained stdlib Python; no third-party imports.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Union

# Known container tag types. An ``otrk`` tag (one per track inside ``database
# V2``) holds a tag stream of per-track fields; ``osrt`` holds sort order;
# ``ovct`` holds a column descriptor inside an ``ovct`` parent. We model the
# minimum set that real Serato DBs emit so round-trip is lossless for the
# fixtures we author.
CONTAINER_TYPES: frozenset[str] = frozenset(
    {
        "otrk",  # one track entry
        "osrt",  # sort order
        "ovct",  # column in the crate header
        "ovnd",  # view-name descriptor
    }
)

_HEADER_STRUCT = struct.Struct(">4sI")  # 4 bytes type + uint32 length


@dataclass(frozen=True)
class RawTag:
    """A leaf tag -- type + opaque payload bytes."""

    type: str
    payload: bytes


@dataclass(frozen=True)
class ContainerTag:
    """A container tag whose payload is a nested tag stream."""

    type: str
    children: tuple[Union["ContainerTag", RawTag], ...]


Tag = Union[RawTag, ContainerTag]


class TagStreamError(ValueError):
    """Raised when a byte stream cannot be decoded as a Serato tag stream."""


class TagStream:
    """Encode + decode Serato tag streams.

    Static methods for stateless use::

        tags = TagStream.read(data)
        out  = TagStream.write(tags)

    Round-trip is byte-identical when every unknown tag is preserved.
    """

    # ------------------------------------------------------------- decoder

    @staticmethod
    def read(data: bytes) -> tuple[Tag, ...]:
        """Decode ``data`` into a tuple of tags. Raises ``TagStreamError``."""
        tags: list[Tag] = []
        pos = 0
        n = len(data)
        while pos < n:
            if n - pos < _HEADER_STRUCT.size:
                raise TagStreamError(
                    f"truncated header at offset {pos}: {n - pos} bytes left"
                )
            raw_type, length = _HEADER_STRUCT.unpack_from(data, pos)
            pos += _HEADER_STRUCT.size
            try:
                type_str = raw_type.decode("ascii")
            except UnicodeDecodeError as exc:
                raise TagStreamError(
                    f"non-ASCII tag type {raw_type!r} at offset {pos - _HEADER_STRUCT.size}"
                ) from exc
            if n - pos < length:
                raise TagStreamError(
                    f"tag {type_str!r} claims {length} bytes but only {n - pos} remain"
                )
            payload = data[pos : pos + length]
            pos += length
            if type_str in CONTAINER_TYPES:
                tags.append(ContainerTag(type=type_str, children=TagStream.read(payload)))
            else:
                tags.append(RawTag(type=type_str, payload=payload))
        return tuple(tags)

    # ------------------------------------------------------------- encoder

    @staticmethod
    def write(tags: Sequence[Tag]) -> bytes:
        """Encode ``tags`` to bytes. Unknown types are written verbatim."""
        chunks: list[bytes] = []
        for tag in tags:
            if isinstance(tag, ContainerTag):
                payload = TagStream.write(tag.children)
            elif isinstance(tag, RawTag):
                payload = tag.payload
            else:
                raise TypeError(f"unsupported tag type: {type(tag).__name__}")
            type_bytes = tag.type.encode("ascii")
            if len(type_bytes) != 4:
                raise TagStreamError(
                    f"tag type must be exactly 4 ASCII bytes; got {tag.type!r}"
                )
            chunks.append(_HEADER_STRUCT.pack(type_bytes, len(payload)))
            chunks.append(payload)
        return b"".join(chunks)


# ----------------------------------------------------------------- helpers


def find(tags: Iterable[Tag], tag_type: str) -> Tag | None:
    """Return the first tag of ``tag_type`` in ``tags``, or None."""
    for tag in tags:
        if tag.type == tag_type:
            return tag
    return None


def find_all(tags: Iterable[Tag], tag_type: str) -> tuple[Tag, ...]:
    """Return every tag of ``tag_type`` in ``tags``."""
    return tuple(t for t in tags if t.type == tag_type)
