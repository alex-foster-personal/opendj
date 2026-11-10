"""LIGHT-9944: the sACN (E1.31) data packet encoder lays out every field where the standard puts it.

[if] a 512-slot packet is not 638 bytes or a field sits at the wrong offset [then] fail, [else stop].
"""

from __future__ import annotations

import json
import struct
import uuid
from pathlib import Path

import pytest

from apps.lighting.sacn import SacnFrame, encode_data_packet, load_or_create_cid, multicast_address

pytestmark = [pytest.mark.requirement("LIGHT-9944")]

CID = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff")


def _frame(**overrides: object) -> SacnFrame:
    fields: dict[str, object] = {"cid": CID, "source_name": "Open DJ", "universe": 1, "slots": bytes(range(256)) * 2}
    fields.update(overrides)
    return SacnFrame(**fields)  # type: ignore[arg-type]


def test_universe_one_with_512_slots_is_638_bytes_with_the_universe_at_113() -> None:
    """[if] universe 1 with 512 slots is not 638 bytes or 113..114 is not the universe [then] fail, [else stop]."""
    packet = encode_data_packet(_frame())
    assert len(packet) == 638
    assert struct.unpack("!H", packet[113:115])[0] == 1


def test_root_framing_and_dmp_layers_hold_the_standard_fields() -> None:
    """[if] any header field is not where E1.31 puts it [then] fail, [else stop]."""
    packet = encode_data_packet(_frame(universe=7, sequence=42, priority=150, sync_address=9))
    assert packet[0:4] == b"\x00\x10\x00\x00"
    assert packet[4:16] == b"ASC-E1.17\x00\x00\x00"
    assert struct.unpack("!H", packet[16:18])[0] == 0x7000 | (638 - 16)
    assert struct.unpack("!I", packet[18:22])[0] == 0x00000004
    assert packet[22:38] == CID.bytes
    assert struct.unpack("!H", packet[38:40])[0] == 0x7000 | (638 - 38)
    assert struct.unpack("!I", packet[40:44])[0] == 0x00000002
    assert packet[44:108] == b"Open DJ".ljust(64, b"\x00")
    assert packet[108] == 150
    assert struct.unpack("!H", packet[109:111])[0] == 9
    assert packet[111] == 42
    assert packet[112] == 0
    assert struct.unpack("!H", packet[113:115])[0] == 7
    assert struct.unpack("!H", packet[115:117])[0] == 0x7000 | (638 - 115)
    assert packet[117] == 0x02
    assert packet[118] == 0xA1
    assert struct.unpack("!HHH", packet[119:125]) == (0, 1, 513)
    assert packet[125] == 0
    assert packet[126:] == bytes(range(256)) * 2


def test_short_universe_shrinks_lengths_and_property_count() -> None:
    """[if] a 4-slot packet keeps 512-slot lengths [then] fail, [else stop]."""
    packet = encode_data_packet(_frame(slots=b"\x01\x02\x03\x04"))
    assert len(packet) == 130
    assert struct.unpack("!H", packet[123:125])[0] == 5
    assert struct.unpack("!H", packet[16:18])[0] == 0x7000 | (130 - 16)


def test_default_priority_is_100() -> None:
    """[if] the default priority is not 100 [then] fail, [else stop]."""
    assert encode_data_packet(_frame())[108] == 100


def test_universe_300_multicasts_to_239_255_1_44() -> None:
    """[if] universe 300 does not map to 239.255.1.44 [then] fail, [else stop]."""
    assert multicast_address(300) == "239.255.1.44"
    assert multicast_address(1) == "239.255.0.1"


def test_terminate_sets_option_bit_6_and_keeps_the_slot_data() -> None:
    """[if] terminate leaves bit 6 clear or drops the slots [then] fail, [else stop]."""
    packet = encode_data_packet(_frame(terminate=True))
    assert packet[112] & 0x40
    assert packet[126:] == bytes(range(256)) * 2
    assert len(packet) == 638


@pytest.mark.parametrize("overrides", [{"priority": 201}, {"universe": 0}, {"universe": 64000}])
def test_out_of_range_priority_or_universe_is_refused(overrides: dict[str, object]) -> None:
    """[if] priority 201 or universe 0 or 64000 builds a frame [then] fail, [else stop]."""
    with pytest.raises(ValueError):
        _frame(**overrides)


@pytest.mark.parametrize("universe", [0, 64000])
def test_multicast_address_refuses_an_out_of_range_universe(universe: int) -> None:
    """[if] multicast_address accepts universe 0 or 64000 [then] fail, [else stop]."""
    with pytest.raises(ValueError):
        multicast_address(universe)


def test_too_many_slots_or_a_long_source_name_is_refused() -> None:
    """[if] 513 slots or a 64-byte source name builds a frame [then] fail, [else stop]."""
    with pytest.raises(ValueError):
        _frame(slots=bytes(513))
    with pytest.raises(ValueError):
        _frame(source_name="x" * 64)


def test_the_same_settings_file_gives_the_same_cid_across_runs(tmp_path: Path) -> None:
    """[if] two runs on one settings file give different CIDs [then] fail, [else stop]."""
    settings = tmp_path / "lighting" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"other": 1}\n')
    first = load_or_create_cid(settings)
    second = load_or_create_cid(settings)
    assert first == second
    assert '"other": 1' in settings.read_text()
    assert load_or_create_cid(tmp_path / "fresh.json") != first


def test_adding_the_cid_keeps_utf8_settings_on_a_cp1252_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] adding the CID on a cp1252 host corrupts a UTF-8 value or writes CRLF [then] fail, [else stop]."""
    real_read_text, real_write_text = Path.read_text, Path.write_text

    def cp1252_read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        return real_read_text(self, encoding=encoding or "cp1252", errors=errors)

    def cp1252_write_text(
        self: Path, data: str, encoding: str | None = None, errors: str | None = None, newline: str | None = None
    ) -> int:
        crlf_default = "\r\n" if newline is None else newline
        return real_write_text(self, data, encoding=encoding or "cp1252", errors=errors, newline=crlf_default)

    monkeypatch.setattr(Path, "read_text", cp1252_read_text)
    monkeypatch.setattr(Path, "write_text", cp1252_write_text)
    settings = tmp_path / "settings.json"
    settings.write_bytes(json.dumps({"source_name": "Café"}, ensure_ascii=False).encode("utf-8"))
    load_or_create_cid(settings)
    raw = settings.read_bytes()
    assert b"\r\n" not in raw
    assert json.loads(raw.decode("utf-8"))["source_name"] == "Café"
