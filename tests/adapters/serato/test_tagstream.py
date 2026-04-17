"""Unit tests for the Serato tag-stream codec (OPEN-02c)."""

from __future__ import annotations

import struct

import pytest

from apps.adapters.serato.tagstream import (
    ContainerTag,
    RawTag,
    TagStream,
    TagStreamError,
    find,
    find_all,
)

pytestmark = pytest.mark.requirement("OPEN-02")


@pytest.mark.requirement("OPEN-02c")
def test_empty_stream_roundtrip() -> None:
    assert TagStream.read(b"") == ()
    assert TagStream.write(()) == b""


@pytest.mark.requirement("OPEN-02c")
def test_single_leaf_tag_roundtrip() -> None:
    data = struct.pack(">4sI", b"tbpm", 5) + b"128.0"
    tags = TagStream.read(data)
    assert tags == (RawTag(type="tbpm", payload=b"128.0"),)
    assert TagStream.write(tags) == data


@pytest.mark.requirement("OPEN-02c")
def test_container_tag_roundtrip() -> None:
    inner = struct.pack(">4sI", b"tsng", 4) + b"TEST"
    payload = struct.pack(">4sI", b"otrk", len(inner)) + inner
    tags = TagStream.read(payload)
    assert isinstance(tags[0], ContainerTag)
    assert tags[0].type == "otrk"
    assert tags[0].children == (RawTag(type="tsng", payload=b"TEST"),)
    assert TagStream.write(tags) == payload


@pytest.mark.requirement("OPEN-02c")
def test_unknown_tag_preserved_verbatim() -> None:
    """An unrecognised ASCII type round-trips as RawTag."""
    data = struct.pack(">4sI", b"xxyy", 3) + b"zzz"
    tags = TagStream.read(data)
    assert tags == (RawTag(type="xxyy", payload=b"zzz"),)
    assert TagStream.write(tags) == data


@pytest.mark.requirement("OPEN-02c")
def test_find_helpers() -> None:
    tags = (
        RawTag(type="tbpm", payload=b"120"),
        RawTag(type="tsng", payload=b"\x00T"),
        RawTag(type="tsng", payload=b"\x00U"),
    )
    assert find(tags, "tbpm") is tags[0]
    assert find(tags, "zzzz") is None
    assert find_all(tags, "tsng") == (tags[1], tags[2])


@pytest.mark.requirement("OPEN-02c")
def test_truncated_header_raises() -> None:
    with pytest.raises(TagStreamError):
        TagStream.read(b"abc")


@pytest.mark.requirement("OPEN-02c")
def test_claimed_length_exceeds_payload() -> None:
    # claim 100 bytes, provide 0
    data = struct.pack(">4sI", b"tbpm", 100)
    with pytest.raises(TagStreamError):
        TagStream.read(data)


@pytest.mark.requirement("OPEN-02c")
def test_write_rejects_non_four_byte_type() -> None:
    with pytest.raises(TagStreamError):
        TagStream.write((RawTag(type="xy", payload=b""),))
