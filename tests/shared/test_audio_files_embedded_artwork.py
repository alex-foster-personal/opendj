"""Tests for :func:`apps.shared.audio_files.read_embedded_artwork`.

Pictures are embedded with stdlib ID3 / FLAC / ffmpeg helpers in
``tests/support/embed_picture.py``. The product does not write tags and
does not depend on mutagen (GPL).
"""
from __future__ import annotations

import shutil
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from apps.shared import audio_files
from tests.fixtures.conftest import resolve_required_fixture
from tests.support.embed_picture import (
    apic_frame,
    embed_m4a_cover,
    insert_flac_picture,
    insert_wav_id3,
    prepend_id3,
    with_id3_apic,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


@pytest.fixture(scope="module")
def jpeg_bytes() -> bytes:
    root = resolve_required_fixture("rb-usb-export")
    artwork_jpeg = root / "PIONEER" / "Artwork" / "00009" / "a169.jpg"
    data = artwork_jpeg.read_bytes()
    assert data[:2] == b"\xff\xd8"
    return data


def _mp3(tmp_path: Path, image: bytes, **kwargs: object) -> Path:
    dst = tmp_path / "t.mp3"
    raw = (FIXTURE_ROOT / "src-320.mp3").read_bytes()
    dst.write_bytes(with_id3_apic(raw, image, **kwargs))  # type: ignore[arg-type]
    return dst


@pytest.mark.requirement("CAT-05")
def test_mp3_apic_round_trips(tmp_path: Path, jpeg_bytes: bytes) -> None:
    dst = _mp3(tmp_path, jpeg_bytes)
    assert audio_files.read_embedded_artwork(dst) == (jpeg_bytes, "image/jpeg")


@pytest.mark.requirement("CAT-05")
def test_flac_picture_round_trips(tmp_path: Path, jpeg_bytes: bytes) -> None:
    dst = tmp_path / "t.flac"
    raw = (FIXTURE_ROOT / "src.flac").read_bytes()
    dst.write_bytes(insert_flac_picture(raw, jpeg_bytes, mime="image/jpeg"))
    assert audio_files.read_embedded_artwork(dst) == (jpeg_bytes, "image/jpeg")


@pytest.mark.requirement("CAT-05")
def test_m4a_covr_round_trips(tmp_path: Path, jpeg_bytes: bytes) -> None:
    dst = tmp_path / "t.m4a"
    embed_m4a_cover(FIXTURE_ROOT / "src.m4a", dst, jpeg_bytes)
    result = audio_files.read_embedded_artwork(dst)
    assert result is not None
    assert result[1] == "image/jpeg"
    assert result[0][:2] == b"\xff\xd8"


@pytest.mark.requirement("CAT-05")
def test_wav_apic_round_trips(tmp_path: Path, jpeg_bytes: bytes) -> None:
    dst = tmp_path / "t.wav"
    raw = (FIXTURE_ROOT / "src.wav").read_bytes()
    frame = apic_frame(jpeg_bytes, mime="image/jpeg")
    dst.write_bytes(insert_wav_id3(raw, [frame]))
    assert audio_files.read_embedded_artwork(dst) == (jpeg_bytes, "image/jpeg")


@pytest.mark.requirement("CAT-05")
def test_no_picture_frame_returns_none(tmp_path: Path) -> None:
    dst = tmp_path / "t.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", dst)
    assert audio_files.read_embedded_artwork(dst) is None


@pytest.mark.requirement("CAT-05")
def test_non_audio_file_returns_none(tmp_path: Path) -> None:
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    assert audio_files.read_embedded_artwork(f) is None


@pytest.mark.requirement("CAT-05")
def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert audio_files.read_embedded_artwork(tmp_path / "missing.mp3") is None


@pytest.mark.requirement("CAT-05")
def test_apic_declaring_non_image_mime_is_rejected(tmp_path: Path, jpeg_bytes: bytes) -> None:
    dst = _mp3(tmp_path, jpeg_bytes, mime="text/html")
    assert audio_files.read_embedded_artwork(dst) is None
    assert audio_files.embedded_artwork_available(dst) is False


@pytest.mark.requirement("CAT-05")
def test_apic_at_embedded_artwork_limit_round_trips(tmp_path: Path) -> None:
    dst = tmp_path / "at-limit.mp3"
    image = BytesIO()
    Image.new("RGB", (1, 1)).save(image, format="JPEG")
    jpeg = image.getvalue()
    bounded = jpeg + b"\x00" * (audio_files.MAX_EMBEDDED_ARTWORK_BYTES - len(jpeg))
    raw = (FIXTURE_ROOT / "src-320.mp3").read_bytes()
    dst.write_bytes(with_id3_apic(raw, bounded, desc="maximum cover"))
    assert audio_files.read_embedded_artwork(dst) == (bounded, "image/jpeg")
    assert audio_files.embedded_artwork_available(dst) is True


@pytest.mark.requirement("CAT-05")
def test_apic_bytes_not_matching_declared_mime_is_rejected(tmp_path: Path) -> None:
    dst = _mp3(tmp_path, b"<script>alert(1)</script>")
    assert audio_files.read_embedded_artwork(dst) is None


@pytest.mark.requirement("CAT-05")
def test_apic_declaring_webp_with_wav_riff_bytes_is_rejected(tmp_path: Path) -> None:
    wav_bytes = (FIXTURE_ROOT / "src.wav").read_bytes()
    assert wav_bytes[:4] == b"RIFF" and wav_bytes[8:12] == b"WAVE"
    dst = _mp3(tmp_path, wav_bytes, mime="image/webp")
    assert audio_files.read_embedded_artwork(dst) is None


@pytest.mark.requirement("CAT-05")
def test_first_unusable_frame_does_not_hide_a_later_valid_cover(
    tmp_path: Path, jpeg_bytes: bytes
) -> None:
    dst = tmp_path / "t.mp3"
    raw = (FIXTURE_ROOT / "src-320.mp3").read_bytes()
    frames = [
        apic_frame(b"not a real image", mime="image/jpeg", desc="unusable first frame"),
        apic_frame(jpeg_bytes, mime="image/jpeg", desc="valid second frame"),
    ]
    dst.write_bytes(prepend_id3(raw, frames))
    assert audio_files.read_embedded_artwork(dst) == (jpeg_bytes, "image/jpeg")


@pytest.mark.requirement("CAT-05")
def test_apic_declaring_png_with_real_jpeg_bytes_is_rejected(
    tmp_path: Path, jpeg_bytes: bytes
) -> None:
    dst = _mp3(tmp_path, jpeg_bytes, mime="image/png")
    assert audio_files.read_embedded_artwork(dst) is None


@pytest.mark.requirement("CAT-05")
def test_apic_declaring_uppercase_mime_still_round_trips(
    tmp_path: Path, jpeg_bytes: bytes
) -> None:
    dst = _mp3(tmp_path, jpeg_bytes, mime="IMAGE/JPEG")
    assert audio_files.read_embedded_artwork(dst) == (jpeg_bytes, "image/jpeg")


@pytest.mark.requirement("CAT-05")
def test_apic_with_empty_mime_and_jpeg_bytes_is_rejected(
    tmp_path: Path, jpeg_bytes: bytes
) -> None:
    dst = _mp3(tmp_path, jpeg_bytes, mime="")
    assert audio_files.read_embedded_artwork(dst) is None


@pytest.mark.requirement("CAT-05")
def test_flac_picture_with_empty_mime_and_jpeg_bytes_is_rejected(
    tmp_path: Path, jpeg_bytes: bytes
) -> None:
    dst = tmp_path / "t.flac"
    raw = (FIXTURE_ROOT / "src.flac").read_bytes()
    dst.write_bytes(insert_flac_picture(raw, jpeg_bytes, mime=""))
    assert audio_files.read_embedded_artwork(dst) is None


@pytest.mark.requirement("CAT-05")
def test_apic_larger_than_embedded_artwork_limit_is_rejected(tmp_path: Path) -> None:
    dst = tmp_path / "oversized.mp3"
    image = BytesIO()
    Image.new("RGB", (1, 1)).save(image, format="JPEG")
    oversized = image.getvalue() + b"\x00" * (4 * 1024 * 1024)
    raw = (FIXTURE_ROOT / "src-320.mp3").read_bytes()
    dst.write_bytes(with_id3_apic(raw, oversized, desc="oversized cover"))
    assert audio_files.read_embedded_artwork(dst) is None
    assert audio_files.embedded_artwork_available(dst) is False
