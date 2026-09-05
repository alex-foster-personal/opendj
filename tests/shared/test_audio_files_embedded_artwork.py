"""Tests for :func:`apps.shared.audio_files.read_embedded_artwork`.

Real fixtures (``tests/fixtures/phase7-dedup``) get a REAL embedded picture
frame written onto a copy via mutagen's own writer -- never synthesised
bytes pretending to be a tag -- then the reader must round-trip the exact
image. Needs the optional ``tags`` extra (mutagen); skips (never fails) when
absent, same as every other ``requires_mutagen`` test.

Regression one-liners:
  - if an mp3 with a real APIC frame doesn't round-trip the exact jpeg bytes then broken
  - if a flac with a real Picture block doesn't round-trip the exact jpeg bytes then broken
  - if an m4a with a real covr atom doesn't round-trip the exact jpeg bytes then broken
  - if a wav with a real APIC frame doesn't round-trip the exact jpeg bytes then broken
  - if a file with no picture frame returns anything but None then broken
  - if a non-audio file crashes the reader instead of returning None then broken
  - if a picture frame declaring a non-image mime (e.g. text/html) is served then broken
  - if a picture frame whose bytes don't match its declared image mime is served then broken
  - if a frame declaring image/png with real jpeg bytes is served
    (mismatched mime/magic pair) then broken
  - if a real wav (RIFF/WAVE) frame declared image/webp is served then broken
  - if the first of multiple picture frames is unusable and a later one is a
    valid front cover then the cover is served, not None
  - if a real cover declaring a case-variant mime (e.g. IMAGE/JPEG) is
    rejected by a case-sensitive allow-list lookup then broken
  - if a picture frame with an empty declared mime is rewritten to
    image/jpeg and served then broken
  - if an embedded picture exceeds the response memory limit then it is
    rejected before the reader copies its payload then broken
"""
from __future__ import annotations

import shutil
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from apps.shared import audio_files
from tests.fixtures.conftest import resolve_required_fixture

pytestmark = pytest.mark.requires_mutagen

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


@pytest.fixture(scope="module")
def jpeg_bytes() -> bytes:
    """Real 80x80 JPEG from the rekordbox USB export fixtures, not synthesised.

    Routes through ``resolve_required_fixture()`` (rather than a hard-coded
    repo path evaluated relative to CWD) so this CAT-05 acceptance test
    keeps finding the fixture, and keeps refusing to silently skip, once
    the in-repo directory leaves and only ``rb-usb-export.extern`` remains
    (PR #718).
    """
    root = resolve_required_fixture("rb-usb-export")
    artwork_jpeg = root / "PIONEER" / "Artwork" / "00009" / "a169.jpg"
    data = artwork_jpeg.read_bytes()
    assert data[:2] == b"\xff\xd8"  # real JPEG SOI marker, not a fake fixture
    return data


@pytest.mark.requirement("CAT-05")
def test_mp3_apic_round_trips(tmp_path: Path, jpeg_bytes: bytes) -> None:
    from mutagen.id3 import APIC
    from mutagen.mp3 import MP3

    dst = tmp_path / "t.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", dst)
    audio = MP3(dst)
    audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="cover", data=jpeg_bytes))
    audio.save()

    result = audio_files.read_embedded_artwork(dst)
    assert result == (jpeg_bytes, "image/jpeg")


@pytest.mark.requirement("CAT-05")
def test_flac_picture_round_trips(tmp_path: Path, jpeg_bytes: bytes) -> None:
    from mutagen.flac import FLAC, Picture

    dst = tmp_path / "t.flac"
    shutil.copy2(FIXTURE_ROOT / "src.flac", dst)
    audio = FLAC(dst)
    pic = Picture()
    pic.data = jpeg_bytes
    pic.type = 3
    pic.mime = "image/jpeg"
    audio.add_picture(pic)
    audio.save()

    result = audio_files.read_embedded_artwork(dst)
    assert result == (jpeg_bytes, "image/jpeg")


@pytest.mark.requirement("CAT-05")
def test_m4a_covr_round_trips(tmp_path: Path, jpeg_bytes: bytes) -> None:
    from mutagen.mp4 import MP4, MP4Cover

    dst = tmp_path / "t.m4a"
    shutil.copy2(FIXTURE_ROOT / "src.m4a", dst)
    audio = MP4(dst)
    audio.tags["covr"] = [MP4Cover(jpeg_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()

    result = audio_files.read_embedded_artwork(dst)
    assert result == (jpeg_bytes, "image/jpeg")


@pytest.mark.requirement("CAT-05")
def test_wav_apic_round_trips(tmp_path: Path, jpeg_bytes: bytes) -> None:
    from mutagen.id3 import APIC
    from mutagen.wave import WAVE

    dst = tmp_path / "t.wav"
    shutil.copy2(FIXTURE_ROOT / "src.wav", dst)
    audio = WAVE(dst)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="cover", data=jpeg_bytes))
    audio.save()

    result = audio_files.read_embedded_artwork(dst)
    assert result == (jpeg_bytes, "image/jpeg")


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
    """A frame that declares e.g. text/html must never reach the HTTP response."""
    from mutagen.id3 import APIC
    from mutagen.mp3 import MP3

    dst = tmp_path / "t.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", dst)
    audio = MP3(dst)
    audio.tags.add(
        APIC(encoding=3, mime="text/html", type=3, desc="cover", data=jpeg_bytes)
    )
    audio.save()

    assert audio_files.read_embedded_artwork(dst) is None
    assert audio_files.embedded_artwork_available(dst) is False


@pytest.mark.requirement("CAT-05")
def test_apic_at_embedded_artwork_limit_round_trips(tmp_path: Path) -> None:
    """The limit is inclusive, preventing an accidental stricter guard."""
    from mutagen.id3 import APIC
    from mutagen.mp3 import MP3

    dst = tmp_path / "at-limit.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", dst)
    image = BytesIO()
    Image.new("RGB", (1, 1)).save(image, format="JPEG")
    jpeg_bytes = image.getvalue()
    bounded_jpeg = jpeg_bytes + b"\x00" * (
        audio_files.MAX_EMBEDDED_ARTWORK_BYTES - len(jpeg_bytes)
    )
    audio = MP3(dst)
    audio.tags.add(
        APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,
            desc="maximum cover",
            data=bounded_jpeg,
        )
    )
    audio.save()

    assert audio_files.read_embedded_artwork(dst) == (bounded_jpeg, "image/jpeg")
    assert audio_files.embedded_artwork_available(dst) is True


@pytest.mark.requirement("CAT-05")
def test_apic_bytes_not_matching_declared_mime_is_rejected(tmp_path: Path) -> None:
    """A frame claiming image/jpeg whose bytes are not a real jpeg must be rejected."""
    from mutagen.id3 import APIC
    from mutagen.mp3 import MP3

    dst = tmp_path / "t.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", dst)
    audio = MP3(dst)
    audio.tags.add(
        APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,
            desc="cover",
            data=b"<script>alert(1)</script>",
        )
    )
    audio.save()

    assert audio_files.read_embedded_artwork(dst) is None


@pytest.mark.requirement("CAT-05")
def test_apic_declaring_webp_with_wav_riff_bytes_is_rejected(tmp_path: Path) -> None:
    """RIFF is a container signature shared with WAV/AVI/etc, not a format
    signature -- a real WAV file's bytes (genuinely RIFF/WAVE, not
    synthesised) declared image/webp must still be rejected because bytes
    8-11 are not WEBP."""
    from mutagen.id3 import APIC
    from mutagen.mp3 import MP3

    wav_bytes = (FIXTURE_ROOT / "src.wav").read_bytes()
    assert wav_bytes[:4] == b"RIFF" and wav_bytes[8:12] == b"WAVE"

    dst = tmp_path / "t.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", dst)
    audio = MP3(dst)
    audio.tags.add(
        APIC(encoding=3, mime="image/webp", type=3, desc="cover", data=wav_bytes)
    )
    audio.save()

    assert audio_files.read_embedded_artwork(dst) is None


@pytest.mark.requirement("CAT-05")
def test_first_unusable_frame_does_not_hide_a_later_valid_cover(
    tmp_path: Path, jpeg_bytes: bytes
) -> None:
    """Multiple embedded pictures are valid and file-controlled ordering is
    not a signal -- an invalid first frame must not make the reader give up
    on a perfectly usable front cover a frame later."""
    from mutagen.id3 import APIC
    from mutagen.mp3 import MP3

    dst = tmp_path / "t.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", dst)
    audio = MP3(dst)
    audio.tags.add(
        APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,
            desc="unusable first frame",
            data=b"not a real image",
        )
    )
    audio.tags.add(
        APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,
            desc="valid second frame",
            data=jpeg_bytes,
        )
    )
    audio.save()

    result = audio_files.read_embedded_artwork(dst)
    assert result == (jpeg_bytes, "image/jpeg")


@pytest.mark.requirement("CAT-05")
def test_apic_declaring_png_with_real_jpeg_bytes_is_rejected(
    tmp_path: Path, jpeg_bytes: bytes
) -> None:
    """A mismatched (mime, magic) pair must be rejected even though the
    magic bytes ARE a real raster image -- just not the declared one. A
    check against the union of all magic numbers rather than the pair
    tied to the declared mime would wrongly accept this."""
    from mutagen.id3 import APIC
    from mutagen.mp3 import MP3

    dst = tmp_path / "t.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", dst)
    audio = MP3(dst)
    audio.tags.add(
        APIC(encoding=3, mime="image/png", type=3, desc="cover", data=jpeg_bytes)
    )
    audio.save()

    assert audio_files.read_embedded_artwork(dst) is None


@pytest.mark.requirement("CAT-05")
def test_apic_declaring_uppercase_mime_still_round_trips(
    tmp_path: Path, jpeg_bytes: bytes
) -> None:
    """Mime tokens are case-insensitive per RFC 2045 -- a real cover declared
    IMAGE/JPEG is the same type as image/jpeg and must not be rejected by a
    case-sensitive allow-list lookup. The served mime is normalized too."""
    from mutagen.id3 import APIC
    from mutagen.mp3 import MP3

    dst = tmp_path / "t.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", dst)
    audio = MP3(dst)
    audio.tags.add(
        APIC(encoding=3, mime="IMAGE/JPEG", type=3, desc="cover", data=jpeg_bytes)
    )
    audio.save()

    result = audio_files.read_embedded_artwork(dst)
    assert result == (jpeg_bytes, "image/jpeg")


@pytest.mark.requirement("CAT-05")
def test_apic_with_empty_mime_and_jpeg_bytes_is_rejected(
    tmp_path: Path, jpeg_bytes: bytes
) -> None:
    """An APIC frame with no declared mime must not be rewritten to
    image/jpeg before validation -- PARITY-04 requires a declared mime
    outside the allow-list, including an absent declaration, to produce
    ARTWORK_NOT_FOUND even when the bytes happen to have JPEG magic."""
    from mutagen.id3 import APIC
    from mutagen.mp3 import MP3

    dst = tmp_path / "t.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", dst)
    audio = MP3(dst)
    audio.tags.add(APIC(encoding=3, mime="", type=3, desc="cover", data=jpeg_bytes))
    audio.save()

    assert audio_files.read_embedded_artwork(dst) is None


@pytest.mark.requirement("CAT-05")
def test_flac_picture_with_empty_mime_and_jpeg_bytes_is_rejected(
    tmp_path: Path, jpeg_bytes: bytes
) -> None:
    """Same fail-closed behavior as the APIC case, for the FLAC Picture path."""
    from mutagen.flac import FLAC, Picture

    dst = tmp_path / "t.flac"
    shutil.copy2(FIXTURE_ROOT / "src.flac", dst)
    audio = FLAC(dst)
    pic = Picture()
    pic.data = jpeg_bytes
    pic.type = 3
    pic.mime = ""
    audio.add_picture(pic)
    audio.save()

    assert audio_files.read_embedded_artwork(dst) is None


@pytest.mark.requirement("CAT-05")
def test_apic_larger_than_embedded_artwork_limit_is_rejected(
    tmp_path: Path,
) -> None:
    """An oversized frame is rejected before the reader copies its payload."""
    from mutagen.id3 import APIC
    from mutagen.mp3 import MP3

    dst = tmp_path / "oversized.mp3"
    shutil.copy2(FIXTURE_ROOT / "src-320.mp3", dst)
    image = BytesIO()
    Image.new("RGB", (1, 1)).save(image, format="JPEG")
    oversized_jpeg = image.getvalue() + b"\x00" * (4 * 1024 * 1024)
    audio = MP3(dst)
    audio.tags.add(
        APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,
            desc="oversized cover",
            data=oversized_jpeg,
        )
    )
    audio.save()

    assert audio_files.read_embedded_artwork(dst) is None
    assert audio_files.embedded_artwork_available(dst) is False
