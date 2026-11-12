"""Build real ``.stem.mp4`` fixtures with ffmpeg for stem_mp4 reader tests."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from apps.stems.stem_mp4 import STEM_MP4_PART_IDS

_STEM_COLORS = {
    "drums": "#FF0000",
    "bass": "#FF8800",
    "other": "#00FF00",
    "vocals": "#0000FF",
}


def _require_tool(name: str) -> str:
    exe = shutil.which(name)
    if exe is None:
        raise RuntimeError(f"{name} is not on PATH")
    return exe


def _make_box(kind: bytes, payload: bytes) -> bytes:
    size = 8 + len(payload)
    return struct.pack(">I", size) + kind + payload


def _find_child_box_offset(data: bytes | bytearray, kind: bytes) -> tuple[int, int, int] | None:
    offset = 0
    end = len(data)
    while offset + 8 <= end:
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        header = 8
        if size == 0:
            size = end - offset
        elif size == 1:
            return None
        if size < header or offset + size > end:
            return None
        box_type = data[offset + 4 : offset + 8]
        if box_type == kind:
            return offset, offset + size, header
        offset += size
    return None


def _locate_top_level_box(data: bytes, kind: bytes) -> tuple[int, int, int] | None:
    offset = 0
    end = len(data)
    while offset + 8 <= end:
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        header = 8
        if size == 0:
            size = end - offset
        elif size == 1:
            if offset + 16 > end:
                return None
            size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
            header = 16
        if size < header or offset + size > end:
            return None
        box_type = data[offset + 4 : offset + 8]
        if box_type == kind:
            return offset, offset + size, header
        offset += size
    return None


def inject_stem_atom(path: Path, stem_json: dict[str, Any] | None = None) -> None:
    """Append ``moov/udta/stem`` JSON to an on-disk MP4 (in-place)."""
    if stem_json is None:
        stem_json = {
            "version": 1,
            "stems": [{"name": name, "color": _STEM_COLORS[name]} for name in STEM_MP4_PART_IDS],
        }
    raw = bytearray(path.read_bytes())
    moov = _locate_top_level_box(bytes(raw), b"moov")
    if moov is None:
        raise RuntimeError(f"{path} has no moov box")
    moov_start, moov_end, moov_header = moov
    stem_payload = json.dumps(stem_json, separators=(",", ":")).encode("utf-8")
    stem_box = _make_box(b"stem", stem_payload)
    children = bytearray(raw[moov_start + moov_header : moov_end])
    udta_offset = _find_child_box_offset(children, b"udta")
    if udta_offset is None:
        append_box = _make_box(b"udta", stem_box)
        new_moov_body = bytes(children) + append_box
    else:
        udta_start, udta_end, udta_header = udta_offset
        udta_children = bytes(children[udta_start + udta_header : udta_end])
        new_udta_body = udta_children + stem_box
        new_udta = struct.pack(">I", udta_header + len(new_udta_body)) + b"udta" + new_udta_body
        children[udta_start:udta_end] = new_udta
        new_moov_body = bytes(children)
    new_size = moov_header + len(new_moov_body)
    if new_size > 0xFFFFFFFF:
        raise RuntimeError("moov would exceed 32-bit box size")
    new_moov = struct.pack(">I", new_size) + b"moov" + new_moov_body
    raw[moov_start:moov_end] = new_moov
    path.write_bytes(raw)


def build_valid_four_part_stem_mp4(
    dest: Path,
    *,
    duration_s: float = 0.5,
    title: str = "Fixture Stem Track",
    artist: str = "Open DJ Tests",
    with_master: bool = True,
) -> Path:
    """Write a five-track (master + four stems) ``.stem.mp4`` with metadata."""
    ffmpeg = _require_tool("ffmpeg")
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    frequencies = {"master": 220, "drums": 300, "bass": 120, "other": 880, "vocals": 440}
    track_names = ["master", *STEM_MP4_PART_IDS] if with_master else list(STEM_MP4_PART_IDS)
    with tempfile.TemporaryDirectory(prefix="stem-mp4-fixture-") as tmp:
        tmp_path = Path(tmp)
        inputs: list[Path] = []
        for name in track_names:
            part_path = tmp_path / f"{name}.m4a"
            freq = frequencies.get(name, 440)
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={freq}:duration={duration_s}:sample_rate=44100",
                    "-ac",
                    "2",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "64k",
                    str(part_path),
                ],
                check=True,
            )
            inputs.append(part_path)
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
        ]
        for part_path in inputs:
            cmd.extend(["-i", str(part_path)])
        for index in range(len(inputs)):
            cmd.extend(["-map", f"{index}:a:0"])
        cmd.extend(
            [
                "-c",
                "copy",
                "-metadata",
                f"title={title}",
                "-metadata",
                f"artist={artist}",
                "-metadata",
                "album=stem-mp4-fixture",
                str(dest),
            ]
        )
        subprocess.run(cmd, check=True)
    inject_stem_atom(dest)
    return dest


def build_three_part_stem_mp4(dest: Path) -> Path:
    """Malformed: only three stem streams, no master."""
    ffmpeg = _require_tool("ffmpeg")
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stem-mp4-bad-") as tmp:
        tmp_path = Path(tmp)
        inputs: list[Path] = []
        for name in STEM_MP4_PART_IDS[:3]:
            part_path = tmp_path / f"{name}.m4a"
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=44100:cl=stereo:d=0.25",
                    "-c:a",
                    "aac",
                    str(part_path),
                ],
                check=True,
            )
            inputs.append(part_path)
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        for part_path in inputs:
            cmd.extend(["-i", str(part_path)])
        for index in range(3):
            cmd.extend(["-map", f"{index}:a:0"])
        cmd.extend(["-c", "copy", str(dest)])
        subprocess.run(cmd, check=True)
    return dest
