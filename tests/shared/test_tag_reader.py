"""TAGIO-01: tag reads through tinytag, for every container folder import allows.

Fixtures are real audio encoded by ffmpeg with the container's own native tags
(see ``tests/fixtures/tagged_audio.py``); nothing here reads back a value that
the code under test wrote.

Regression one-liners:
  - if an ffmpeg-tagged mp3/flac/ogg/opus/m4a/aiff/wav loses artist or genre then broken
  - if a tag BPM / key / ISRC is dropped for a format that carries it then broken
  - if a real COMM frame is not read as the comment then broken
  - if a non-numeric BPM tag becomes a number then broken
  - if a non-audio or missing file reads as an empty record instead of raising then broken
  - if embedded artwork in mp3/flac/m4a/wav is not found then broken
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from apps.shared import audio_files, id3v2, tag_reader
from tests.fixtures import tagged_audio as ta

pytestmark = [pytest.mark.requirement("TAGIO-01"), pytest.mark.requires_ffmpeg]

# Which fields each container's ffmpeg muxer can carry. ``None`` = not written.
_EXPECTED_EXTRAS: dict[str, tuple[str | None, str | None, str | None]] = {
    # fmt: (bpm, key, isrc)
    "mp3-v24": (ta.BPM, ta.KEY, ta.ISRC),
    "mp3-v23": (ta.BPM, ta.KEY, ta.ISRC),
    "flac": (ta.BPM, ta.KEY, ta.ISRC),
    "ogg": (ta.BPM, ta.KEY, ta.ISRC),
    "opus": (ta.BPM, ta.KEY, ta.ISRC),
    "m4a": (ta.BPM, None, None),
    "aiff": (ta.BPM, ta.KEY, ta.ISRC),
    "wav": (None, None, None),
}


@pytest.fixture(scope="module")
def fixture_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("tagged")


@pytest.mark.parametrize("fmt", ta.FORMATS)
def test_standard_tags_are_read_from_every_container(fixture_dir: Path, fmt: str) -> None:
    """[if] a container carries title/artist/album/genre [then] all are read, [else stop]."""
    tags = tag_reader.read_tags(ta.make_tagged_audio(fixture_dir, fmt))
    assert tags.title == ta.STANDARD_TAGS["title"]
    assert tags.artist == ta.STANDARD_TAGS["artist"]
    assert tags.album == ta.STANDARD_TAGS["album"]
    assert tags.genre == ta.STANDARD_TAGS["genre"]
    assert tags.comment == ta.STANDARD_TAGS["comment"]
    assert tags.duration_s is not None and 0.9 < tags.duration_s < 1.2


@pytest.mark.parametrize("fmt", ta.FORMATS)
def test_bpm_key_and_isrc_are_read_where_the_container_carries_them(
    fixture_dir: Path, fmt: str
) -> None:
    """[if] a file carries BPM / key / ISRC tags [then] each is read, [else stop]."""
    bpm, key, isrc = _EXPECTED_EXTRAS[fmt]
    tags = tag_reader.read_tags(ta.make_tagged_audio(fixture_dir, fmt))
    assert tags.bpm == (float(bpm) if bpm else None)
    assert tags.key == key
    assert tags.isrc == isrc


def test_a_real_comm_frame_is_the_comment(tmp_path: Path) -> None:
    """[if] an mp3 carries a COMM frame [then] it is read as the comment, [else stop]."""
    mp3 = ta.make_tagged_audio(tmp_path, "mp3-v23", tags={"title": "x"})
    ta.add_comm(mp3, "Energy 7 - peak time")
    assert tag_reader.read_tags(mp3).comment == "Energy 7 - peak time"


def test_a_non_numeric_bpm_tag_is_none_not_a_guess(tmp_path: Path) -> None:
    """[if] the BPM tag is not a number [then] bpm is None, [else stop]."""
    mp3 = ta.make_tagged_audio(tmp_path, "mp3-v24", tags={"TBPM": "fast"})
    assert tag_reader.read_tags(mp3).bpm is None


def test_a_decimal_comma_bpm_is_read(tmp_path: Path) -> None:
    """[if] a BPM tag uses a decimal comma [then] it is read as a float, [else stop]."""
    flac = ta.make_tagged_audio(tmp_path, "flac", tags={"BPM": "127,5"})
    assert tag_reader.read_tags(flac).bpm == 127.5


def test_an_untagged_file_reads_as_none_fields_with_a_duration(tmp_path: Path) -> None:
    """[if] a parseable file has no tags [then] fields are None, duration set, [else stop]."""
    tags = tag_reader.read_tags(ta.make_untagged_audio(tmp_path, "flac"))
    assert (tags.title, tags.artist, tags.genre, tags.bpm, tags.key) == (None,) * 5
    assert tags.duration_s is not None


def test_a_non_audio_file_raises_naming_the_file(tmp_path: Path) -> None:
    """[if] the file is not audio [then] TagReadError names it, [else stop]."""
    notes = tmp_path / "notes.mp3"
    notes.write_text("not audio at all")
    with pytest.raises(tag_reader.TagReadError) as excinfo:
        tag_reader.read_tags(notes)
    assert excinfo.value.path == notes


def test_a_missing_file_raises(tmp_path: Path) -> None:
    """[if] the file does not exist [then] TagReadError is raised, [else stop]."""
    with pytest.raises(tag_reader.TagReadError):
        tag_reader.read_tags(tmp_path / "missing.flac")


def test_read_metadata_carries_bpm_key_and_isrc(fixture_dir: Path) -> None:
    """[if] audio_files reads a tagged flac [then] bpm/key/isrc are carried, [else stop]."""
    meta = audio_files.read_metadata(ta.make_tagged_audio(fixture_dir, "flac"))
    assert meta is not None
    assert (meta.artist, meta.genre) == (ta.STANDARD_TAGS["artist"], ta.STANDARD_TAGS["genre"])
    assert (meta.bpm, meta.key, meta.isrc) == (float(ta.BPM), ta.KEY, ta.ISRC)


# ----- artwork ---------------------------------------------------------------
def _jpeg() -> bytes:
    out = BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(out, format="JPEG")
    return out.getvalue()


@pytest.mark.parametrize("fmt", ["mp3-v24", "flac", "m4a"])
def test_ffmpeg_attached_cover_is_read(tmp_path: Path, fmt: str) -> None:
    """[if] ffmpeg attaches a front cover [then] the exact bytes are read, [else stop]."""
    image = tmp_path / "cover.jpg"
    image.write_bytes(_jpeg())
    audio = ta.make_tagged_audio(tmp_path, fmt)
    covered = ta.attach_cover_with_ffmpeg(audio, image, tmp_path / f"covered{audio.suffix}")
    pictures = list(tag_reader.embedded_pictures(covered))
    assert [(data, mime, kind) for data, mime, kind in pictures] == [
        (image.read_bytes(), "image/jpeg", tag_reader.FRONT_COVER_PICTURE_TYPE)
    ]


def test_wav_id3_chunk_apic_is_read(tmp_path: Path) -> None:
    """[if] a wav carries an id3 chunk with an APIC frame [then] it is read, [else stop]."""
    wav = ta.make_tagged_audio(tmp_path, "wav")
    jpeg = _jpeg()
    ta.append_wav_id3_chunk(wav, [id3v2.Frame("APIC", id3v2.encode_apic("image/jpeg", 3, "", jpeg))])
    assert audio_files.read_embedded_artwork(wav) == (jpeg, "image/jpeg")
