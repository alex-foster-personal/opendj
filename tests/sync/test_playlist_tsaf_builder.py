"""SYNC-03: TSAF playlist-blob builder + page-data packer tests."""
from __future__ import annotations

import struct

import pytest

from apps.sync.playlist_tsaf import (
    PLAYLIST_TYPE_LEAF,
    PLAYLIST_TYPE_ROOT,
    build_page_data,
    build_playlist_blob,
    new_page_key,
    parse_page_data,
    parse_playlist_blob,
)


@pytest.mark.requirement("SYNC-03")
def test_page_data_pack_unpack_round_trip_empty() -> None:
    data = build_page_data([])
    assert data == b""
    assert parse_page_data(data) == []


@pytest.mark.requirement("SYNC-03")
def test_page_data_one_member_is_8_bytes_le() -> None:
    data = build_page_data([42])
    assert data == struct.pack("<q", 42)
    assert len(data) == 8
    assert parse_page_data(data) == [42]


@pytest.mark.requirement("SYNC-03")
def test_page_data_round_trip_for_1024_rowids() -> None:
    rowids = list(range(1, 1025))
    data = build_page_data(rowids)
    assert len(data) == 1024 * 8
    assert parse_page_data(data) == rowids


@pytest.mark.requirement("SYNC-03")
def test_parse_page_data_rejects_unaligned_input() -> None:
    assert parse_page_data(b"\x01\x02\x03") == []
    assert parse_page_data(None) == []
    assert parse_page_data(b"") == []


@pytest.mark.requirement("SYNC-03")
def test_new_page_key_is_32_hex_chars() -> None:
    k = new_page_key()
    assert len(k) == 32
    int(k, 16)


@pytest.mark.requirement("SYNC-03")
def test_playlist_blob_has_tsaf_magic_and_class_marker() -> None:
    blob = build_playlist_blob("deadbeef" * 4, "Warmup", kind="leaf")
    assert blob[:4] == b"TSAF"
    assert b"ADCMediaItemPlaylist" in blob


@pytest.mark.requirement("SYNC-03")
def test_playlist_blob_round_trip_extracts_uuid_name_type() -> None:
    uuid_hex = "0123456789abcdef" * 2
    blob = build_playlist_blob(uuid_hex, "Events / 2024 / Berlin", kind="leaf")
    parsed = parse_playlist_blob(blob)
    assert parsed["uuid"] == uuid_hex
    assert parsed["name"] == "Events / 2024 / Berlin"
    assert parsed["type"] == PLAYLIST_TYPE_LEAF


@pytest.mark.requirement("SYNC-03")
def test_playlist_blob_root_kind_uses_root_type_byte() -> None:
    blob = build_playlist_blob("a" * 32, "root", kind="root")
    parsed = parse_playlist_blob(blob)
    assert parsed["type"] == PLAYLIST_TYPE_ROOT


@pytest.mark.requirement("SYNC-03")
def test_playlist_blob_leaf_type_override() -> None:
    blob = build_playlist_blob("b" * 32, "Mix", kind="leaf", leaf_type_byte=0x2A)
    parsed = parse_playlist_blob(blob)
    assert parsed["type"] == 0x2A


@pytest.mark.requirement("SYNC-03")
def test_playlist_blob_rejects_empty_uuid() -> None:
    with pytest.raises(ValueError):
        build_playlist_blob("", "Mix")


@pytest.mark.requirement("SYNC-03")
def test_parse_playlist_blob_returns_empty_for_non_tsaf() -> None:
    assert parse_playlist_blob(b"\x00" * 32) == {}
    assert parse_playlist_blob(b"NOPE" + b"\x00" * 30) == {}


@pytest.mark.requirement("SYNC-03")
def test_playlist_blob_encodes_unicode_name_roundtrip() -> None:
    name = "Café -- Peak Hour"
    blob = build_playlist_blob("c" * 32, name, kind="leaf")
    parsed = parse_playlist_blob(blob)
    assert parsed["name"] == name
