"""sACN (ANSI E1.31) data packet encoder, built clean-room from the public ESTA standard.

Pure bytes in, bytes out: no sockets, no discovery or sync packets. One data packet is the
root layer (ACN identifier, CID), the framing layer (source name, priority, sequence,
options, universe) and the DMP layer (start code then the DMX slots). Spec item L23 in
``(private path)``.
"""

from __future__ import annotations

import json
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path

# ----- constants (ANSI E1.31-2018 section 4 to 7)

PREAMBLE_SIZE = 0x0010
POSTAMBLE_SIZE = 0x0000
ACN_PACKET_IDENTIFIER = b"ASC-E1.17\x00\x00\x00"
FLAGS_HIGH_NIBBLE = 0x7000
VECTOR_ROOT_E131_DATA = 0x00000004
VECTOR_E131_DATA_PACKET = 0x00000002
VECTOR_DMP_SET_PROPERTY = 0x02
DMP_ADDRESS_AND_DATA_TYPE = 0xA1
DMP_FIRST_PROPERTY_ADDRESS = 0x0000
DMP_ADDRESS_INCREMENT = 0x0001
DMX_NULL_START_CODE = 0x00

SOURCE_NAME_BYTES = 64
DEFAULT_PRIORITY = 100
MAX_PRIORITY = 200
MIN_UNIVERSE = 1
MAX_UNIVERSE = 63999
MAX_SLOTS = 512
OPTION_STREAM_TERMINATED = 0x40  # bit 6
OPTION_PREVIEW_DATA = 0x80  # bit 7

ROOT_LAYER_OFFSET = 16  # flags and length count from here (after preamble, postamble, ACN id)
FRAMING_LAYER_OFFSET = 38
UNIVERSE_OFFSET = 113
DMP_LAYER_OFFSET = 115
SLOTS_OFFSET = 126

CID_SETTINGS_KEY = "sacn_cid"


@dataclass(frozen=True)
class SacnFrame:
    """One sACN data packet's fields, validated on construction."""

    cid: uuid.UUID
    source_name: str
    universe: int
    slots: bytes
    sequence: int = 0
    priority: int = DEFAULT_PRIORITY
    sync_address: int = 0
    terminate: bool = False
    preview: bool = False

    def __post_init__(self) -> None:
        if not MIN_UNIVERSE <= self.universe <= MAX_UNIVERSE:
            raise ValueError(f"universe must be {MIN_UNIVERSE}..{MAX_UNIVERSE}, got {self.universe}")
        if not 0 <= self.priority <= MAX_PRIORITY:
            raise ValueError(f"priority must be 0..{MAX_PRIORITY}, got {self.priority}")
        if not 0 <= self.sequence <= 255:
            raise ValueError(f"sequence must be 0..255, got {self.sequence}")
        if not 0 <= self.sync_address <= MAX_UNIVERSE:
            raise ValueError(f"sync_address must be 0 (none) or {MIN_UNIVERSE}..{MAX_UNIVERSE}, got {self.sync_address}")
        if len(self.slots) > MAX_SLOTS:
            raise ValueError(f"at most {MAX_SLOTS} slots, got {len(self.slots)}")
        if len(self.source_name.encode("utf-8")) >= SOURCE_NAME_BYTES:
            raise ValueError(f"source_name must encode to under {SOURCE_NAME_BYTES} UTF-8 bytes (null terminated)")


# ----- encoding


def _flags_and_length(length: int) -> bytes:
    return struct.pack("!H", FLAGS_HIGH_NIBBLE | length)


def _options(frame: SacnFrame) -> int:
    return (OPTION_PREVIEW_DATA if frame.preview else 0) | (OPTION_STREAM_TERMINATED if frame.terminate else 0)


def encode_data_packet(frame: SacnFrame) -> bytes:
    """The E1.31 data packet for ``frame``: 126 header bytes then the slots (638 for 512)."""
    total = SLOTS_OFFSET + len(frame.slots)
    root = (
        struct.pack("!HH", PREAMBLE_SIZE, POSTAMBLE_SIZE)
        + ACN_PACKET_IDENTIFIER
        + _flags_and_length(total - ROOT_LAYER_OFFSET)
        + struct.pack("!I", VECTOR_ROOT_E131_DATA)
        + frame.cid.bytes
    )
    framing = (
        _flags_and_length(total - FRAMING_LAYER_OFFSET)
        + struct.pack("!I", VECTOR_E131_DATA_PACKET)
        + frame.source_name.encode("utf-8").ljust(SOURCE_NAME_BYTES, b"\x00")
        + struct.pack("!BHBBH", frame.priority, frame.sync_address, frame.sequence, _options(frame), frame.universe)
    )
    dmp = (
        _flags_and_length(total - DMP_LAYER_OFFSET)
        + struct.pack(
            "!BBHHHB",
            VECTOR_DMP_SET_PROPERTY,
            DMP_ADDRESS_AND_DATA_TYPE,
            DMP_FIRST_PROPERTY_ADDRESS,
            DMP_ADDRESS_INCREMENT,
            len(frame.slots) + 1,
            DMX_NULL_START_CODE,
        )
        + frame.slots
    )
    packet = root + framing + dmp
    assert len(packet) == total, f"encoder built {len(packet)} bytes, expected {total}"
    return packet


def multicast_address(universe: int) -> str:
    """The E1.31 multicast group for ``universe``: 239.255.<high byte>.<low byte>."""
    if not MIN_UNIVERSE <= universe <= MAX_UNIVERSE:
        raise ValueError(f"universe must be {MIN_UNIVERSE}..{MAX_UNIVERSE}, got {universe}")
    return f"239.255.{universe >> 8}.{universe & 0xFF}"


# ----- CID persistence


def load_or_create_cid(settings_path: Path) -> uuid.UUID:
    """The source CID stored in the lighting settings file, generated and written on first use.

    A source keeps one CID for its lifetime so receivers see one stable sender; other keys in
    the settings file are kept as they are.
    """
    settings: dict[str, object] = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    if not isinstance(settings, dict):
        raise TypeError(f"{settings_path} must hold a JSON object")
    stored = settings.get(CID_SETTINGS_KEY)
    if stored is not None:
        return uuid.UUID(str(stored))
    cid = uuid.uuid4()
    settings[CID_SETTINGS_KEY] = str(cid)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n")
    return cid
