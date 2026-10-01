"""Real, tagged audio fixtures for the tag reader / writer tests.

Two independent producers, so no test reads back only what our own code wrote:

* ``ffmpeg`` encodes real audio and writes the container's native tags
  (ID3v2.3/2.4 for MP3, Vorbis comments for FLAC / OGG / Opus, iTunes atoms
  for M4A, an ID3 chunk for AIFF, LIST/INFO for WAV) and cover art.
* The in-house ``apps.shared.id3v2`` / ``apps.shared.flac_meta`` writers add
  the frames ffmpeg cannot produce (a real COMM frame, APIC frames declaring
  arbitrary mimes, FLAC PICTURE blocks, a WAV ``id3 `` chunk). Their output is
  then read by tinytag, which is the independent oracle.

Every helper raises when ffmpeg fails: a fixture that silently came out empty
would let a reader test pass by reading nothing.
"""
from __future__ import annotations

import json
import shutil
import struct
import subprocess
from pathlib import Path

from apps.shared import flac_meta, id3v2

#: Tag values every format fixture carries. Unicode on purpose.
STANDARD_TAGS: dict[str, str] = {
    "title": "Nachtfahrt ☾",
    "artist": "Kölsch Ensemble",
    "album": "Tags Are Not Optional",
    "genre": "Deep House",
    "comment": "ripped from vinyl",
}
BPM = "124"
KEY = "8A"
ISRC = "GBABC2600001"


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _ffmpeg(args: list[str]) -> None:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *args], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({result.returncode}): {result.stderr.strip()}")


def _metadata_args(tags: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in tags.items():
        args += ["-metadata", f"{key}={value}"]
    return args


# Per-format: (file suffix, codec / muxer args, extra tag keys for bpm / key / isrc).
_FORMAT_ARGS: dict[str, tuple[str, list[str], dict[str, str]]] = {
    "mp3-v24": (".mp3", ["-c:a", "libmp3lame", "-id3v2_version", "4"], {"TBPM": BPM, "TKEY": KEY, "TSRC": ISRC}),
    "mp3-v23": (".mp3", ["-c:a", "libmp3lame", "-id3v2_version", "3"], {"TBPM": BPM, "TKEY": KEY, "TSRC": ISRC}),
    "flac": (".flac", ["-c:a", "flac"], {"BPM": BPM, "INITIALKEY": KEY, "ISRC": ISRC}),
    "ogg": (".ogg", ["-c:a", "libvorbis"], {"BPM": BPM, "INITIALKEY": KEY, "ISRC": ISRC}),
    "opus": (".opus", ["-c:a", "libopus"], {"BPM": BPM, "INITIALKEY": KEY, "ISRC": ISRC}),
    "m4a": (".m4a", ["-c:a", "aac"], {"tmpo": BPM}),
    # moov in front of mdat: a tag write that grows moov must move chunk offsets.
    "m4a-faststart": (".m4a", ["-c:a", "aac", "-movflags", "+faststart"], {"tmpo": BPM}),
    "aiff": (".aiff", ["-write_id3v2", "1"], {"TBPM": BPM, "TKEY": KEY, "TSRC": ISRC}),
    "wav": (".wav", [], {}),
}
FORMATS: tuple[str, ...] = tuple(_FORMAT_ARGS)


def make_tagged_audio(directory: Path, fmt: str, *, tags: dict[str, str] | None = None) -> Path:
    """A 1 s sine in ``fmt`` carrying :data:`STANDARD_TAGS` plus BPM/key/ISRC
    where the container's ffmpeg muxer can write them."""
    suffix, codec_args, extra = _FORMAT_ARGS[fmt]
    path = directory / f"{fmt}{suffix}"
    all_tags = {**STANDARD_TAGS, **extra} if tags is None else tags
    _ffmpeg(
        ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", *codec_args, *_metadata_args(all_tags), str(path)]
    )
    return path


def make_untagged_audio(directory: Path, fmt: str) -> Path:
    suffix, codec_args, _extra = _FORMAT_ARGS[fmt]
    path = directory / f"untagged-{fmt}{suffix}"
    _ffmpeg(
        [
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            *codec_args, "-map_metadata", "-1", "-fflags", "+bitexact", str(path),
        ]
    )
    return path


def attach_cover_with_ffmpeg(audio: Path, image: Path, out: Path) -> Path:
    """Re-mux ``audio`` with ``image`` as an attached front-cover picture."""
    _ffmpeg(
        [
            "-i", str(audio), "-i", str(image), "-map", "0:a", "-map", "1:v",
            "-c", "copy", "-disposition:v", "attached_pic",
            # ffmpeg maps this stream comment to the picture type (3, front cover).
            "-metadata:s:v", "comment=Cover (front)", str(out),
        ]
    )
    return out


def add_apic(mp3: Path, data: bytes, *, mime: str, picture_type: int = 3, desc: str = "cover") -> None:
    tag = id3v2.load_or_new(mp3)
    tag.frames.append(id3v2.Frame("APIC", id3v2.encode_apic(mime, picture_type, desc, data)))
    id3v2.save(mp3, tag)


def add_comm(mp3: Path, text: str, *, description: str = "", language: str = "eng") -> None:
    tag = id3v2.load_or_new(mp3)
    tag.frames.append(id3v2.Frame("COMM", id3v2.encode_comm(language, description, text, tag.version)))
    id3v2.save(mp3, tag)


def add_flac_picture(flac: Path, data: bytes, *, mime: str, picture_type: int = 3) -> None:
    meta = flac_meta.read(flac)
    meta.add_picture(mime, picture_type, data)
    flac_meta.save(flac, meta)


def append_wav_id3_chunk(wav: Path, frames: list[id3v2.Frame], *, version: int = 3) -> None:
    """Append a RIFF ``id3 `` chunk (how WAV carries ID3) and fix the RIFF size."""
    tag = id3v2.render(id3v2.Id3Tag(version=version, frames=frames), padding=0)
    chunk = b"id3 " + struct.pack("<I", len(tag)) + tag + (b"\x00" if len(tag) % 2 else b"")
    raw = bytearray(wav.read_bytes())
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"{wav} is not a RIFF/WAVE file")
    raw += chunk
    raw[4:8] = struct.pack("<I", len(raw) - 8)
    wav.write_bytes(bytes(raw))


def decoded_audio_sha256(path: Path) -> str:
    """sha256 of the PCM ffmpeg decodes from ``path``: identical audio, identical hash."""
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a", "-f", "hash", "-hash", "sha256", "-"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0 or result.stderr.strip() or not result.stdout.startswith("SHA256="):
        raise RuntimeError(f"ffmpeg could not decode {path} cleanly: {result.stderr.strip()}")
    return result.stdout.strip().removeprefix("SHA256=")


def ffprobe_tags(path: Path) -> dict[str, str]:
    """Container and stream tags as ffprobe (an independent parser) sees them, keys lowercased."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags:stream_tags", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {result.stderr.strip()}")
    doc = json.loads(result.stdout)
    tags: dict[str, str] = {}
    for stream in doc.get("streams", []):
        tags.update({k.lower(): v for k, v in stream.get("tags", {}).items()})
    tags.update({k.lower(): v for k, v in doc.get("format", {}).get("tags", {}).items()})
    return tags


def audio_after_id3(mp3: Path) -> bytes:
    """The bytes after the leading ID3v2 tag: the part a tag write must not touch."""
    tag = id3v2.read_tag(mp3)
    data = mp3.read_bytes()
    return data[tag.original_size:] if tag is not None else data


def flac_audio_frames(flac: Path) -> bytes:
    return flac.read_bytes()[flac_meta.read(flac).audio_offset:]


__all__ = [
    "BPM",
    "FORMATS",
    "ISRC",
    "KEY",
    "STANDARD_TAGS",
    "add_apic",
    "add_comm",
    "add_flac_picture",
    "append_wav_id3_chunk",
    "attach_cover_with_ffmpeg",
    "audio_after_id3",
    "decoded_audio_sha256",
    "ffmpeg_available",
    "ffprobe_tags",
    "flac_audio_frames",
    "make_tagged_audio",
    "make_untagged_audio",
]
