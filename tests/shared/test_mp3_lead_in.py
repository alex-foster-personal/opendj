"""MP3 encoder lead-in reader (NAE-22): what our decoders trim and rekordbox keeps.

[if] an MP3's first frame carries a LAME/Lavc/Lavf Xing or Info tag [then] the lead-in is the encoder delay plus 529 decoder samples, exactly what ffmpeg's default decode trims, and a file whose tag symphonia would not honor reads 0, [else stop].

Regression one-liners:
  - if the reader disagrees with ffmpeg's trim on a tagged fixture then broken
  - if an unknown encoder string still reads a lead-in then broken (overshoot)
  - if a LAME tag with a bad CRC still reads a lead-in then broken
  - if a LAME tag with its CRC fixed reads 0 then broken (overshoot the other way)
  - if a leading ID3v2 tag, or two, hides the first frame then broken
  - if a non-MP3 or a missing rekordbox file reads a lead-in then broken
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from apps.shared import mp3_lead_in
from apps.shared.mp3_lead_in import (
    DECODER_DELAY,
    LeadIn,
    _crc16_arc,
    read_lead_in,
    rekordbox_lead_in_s,
    to_our_ms,
    to_rekordbox_s,
)

pytestmark = [pytest.mark.requirement("NAE-22")]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TAGGED = FIXTURES / "phase7-dedup" / "src-128.mp3"
#: Every fixture whose audio starts with sound, so ffmpeg's trim is measurable.
CROSS_CHECKED = (
    TAGGED,
    FIXTURES / "phase7-dedup" / "src-320.mp3",
    FIXTURES / "phase7-dedup" / "src-v2.mp3",
    FIXTURES / "conformance" / "03-8-hot-cues" / "audio" / "cues.mp3",
)


def _tag_offset(data: bytes) -> int:
    for tag in (b"Info", b"Xing"):
        at = data.find(tag)
        if at >= 0:
            return at
    raise AssertionError("fixture has no Xing/Info tag")


def _encoder_at(data: bytes) -> int:
    """Byte offset of the LAME extension's encoder field in the first frame."""
    at = _tag_offset(data)
    flags = int.from_bytes(data[at + 4 : at + 8], "big")
    pos = at + 8
    pos += 4 * bool(flags & 1) + 4 * bool(flags & 2) + 100 * bool(flags & 4) + 4 * bool(flags & 8)
    return pos


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    out = tmp_path / name
    out.write_bytes(data)
    return out


def test_tagged_fixture_reads_the_encoder_delay_plus_the_decoder_delay() -> None:
    lead = read_lead_in(TAGGED)
    assert lead.encoder is not None and lead.encoder.startswith("Lavc")
    assert lead.frames == 576 + DECODER_DELAY == 1105
    assert lead.sample_rate == 22_050
    assert lead.seconds == pytest.approx(1105 / 22_050)


FFMPEG = shutil.which("ffmpeg")


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg to decode")
@pytest.mark.parametrize("path", CROSS_CHECKED, ids=lambda p: p.name)
def test_lead_in_is_exactly_what_ffmpeg_trims(path: Path) -> None:
    """An independent decoder agrees: the trimmed decode is the raw one, lead-in samples in."""
    np = pytest.importorskip("numpy")
    assert FFMPEG is not None

    def decode(*flags: str):
        out = subprocess.run(
            [FFMPEG, "-v", "error", *flags, "-i", str(path), "-f", "s16le", "-ac", "1", "-"],
            capture_output=True,
            check=True,
        ).stdout
        return np.frombuffer(out, dtype=np.int16)

    raw, trimmed = decode("-flags2", "+skip_manual"), decode()
    window = trimmed[2000:6000]
    assert window.any(), "control: the compared window must hold sound, or any shift matches"
    shifts = [k for k in range(3000) if np.array_equal(raw[2000 + k : 6000 + k], window)]
    assert shifts == [read_lead_in(path).frames]


def test_an_encoder_symphonia_does_not_know_trims_nothing(tmp_path: Path) -> None:
    data = bytearray(TAGGED.read_bytes())
    at = _encoder_at(bytes(data))
    data[at : at + 4] = b"Xyzw"
    lead = read_lead_in(_write(tmp_path, "unknown.mp3", bytes(data)))
    assert lead.frames == 0
    assert lead.sample_rate == 22_050, "control: the frame itself still parsed"


def test_a_lame_tag_is_honored_only_when_its_crc_matches(tmp_path: Path) -> None:
    data = bytearray(TAGGED.read_bytes())
    at = _encoder_at(bytes(data))
    data[at : at + 4] = b"LAME"  # LAME always carries a CRC, now stale
    crc_at = at + 24 + 10
    data[crc_at : crc_at + 2] = (0x1234).to_bytes(2, "big")
    assert read_lead_in(_write(tmp_path, "bad.mp3", bytes(data))).frames == 0

    # The same tag with its CRC recomputed is a LAME tag again.
    start = _first_frame_start(bytes(data))
    data[crc_at : crc_at + 2] = _crc16_arc(bytes(data[start:crc_at])).to_bytes(2, "big")
    assert read_lead_in(_write(tmp_path, "good.mp3", bytes(data))).frames == 1105


def _first_frame_start(data: bytes) -> int:
    """Start of the frame holding the Xing/Info tag (sync word before the tag)."""
    at = _tag_offset(data)
    for start in range(at - 4, -1, -1):
        if data[start] == 0xFF and data[start + 1] & 0xE0 == 0xE0:
            return start
    raise AssertionError("no frame sync before the tag")


def test_a_written_crc_of_zero_means_ignore_it(tmp_path: Path) -> None:
    data = bytearray(TAGGED.read_bytes())
    at = _encoder_at(bytes(data))
    data[at : at + 4] = b"LAME"
    crc_at = at + 24 + 10
    data[crc_at : crc_at + 2] = b"\0\0"
    assert read_lead_in(_write(tmp_path, "zero.mp3", bytes(data))).frames == 1105


def test_no_xing_or_info_tag_trims_nothing(tmp_path: Path) -> None:
    data = bytearray(TAGGED.read_bytes())
    at = _tag_offset(bytes(data))
    data[at : at + 4] = b"Junk"
    lead = read_lead_in(_write(tmp_path, "untagged.mp3", bytes(data)))
    assert lead == LeadIn(frames=0, sample_rate=22_050, encoder=None)


def test_stacked_id3v2_tags_are_skipped_before_the_first_frame(tmp_path: Path) -> None:
    def id3(body_len: int, footer: bool = False) -> bytes:
        size = bytes((body_len >> s) & 0x7F for s in (21, 14, 7, 0))
        head = b"ID3\x04\x00" + bytes([0x10 if footer else 0]) + size
        return head + b"\0" * body_len + (b"3DI\x04\x00\x10" + size if footer else b"")

    data = id3(70_000) + id3(300, footer=True) + TAGGED.read_bytes()
    assert read_lead_in(_write(tmp_path, "id3.mp3", data)).frames == 1105


def test_non_mp3_reads_no_lead_in(tmp_path: Path) -> None:
    other = _write(tmp_path, "same-bytes.m4a", TAGGED.read_bytes())
    assert read_lead_in(other) == mp3_lead_in.NO_LEAD_IN


def test_rekordbox_folder_path_without_a_local_file_is_unknown_not_zero(tmp_path: Path) -> None:
    assert rekordbox_lead_in_s(str(tmp_path / "gone.mp3")) is None
    assert rekordbox_lead_in_s(None) is None
    assert rekordbox_lead_in_s("spotify:track:abc") is None
    assert rekordbox_lead_in_s(str(TAGGED)) == pytest.approx(1105 / 22_050)


def test_conversions_round_trip_and_floor_at_zero() -> None:
    lead = 1105 / 44_100
    assert to_our_ms(1025, lead) == 1000
    assert to_our_ms(10, lead) == 0
    assert to_rekordbox_s(1.0, lead) == pytest.approx(1.02506, abs=1e-5)
