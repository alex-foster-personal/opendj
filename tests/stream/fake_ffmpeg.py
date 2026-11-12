"""Stand-in ffmpeg for RTMP sink tests: capability probes, argv record, stdin count."""

from __future__ import annotations

import json
import os
import sys

_FULL_PROTOCOLS = (
    "Supported file protocols:\nInput:\n  file\n  pipe\n  rtmp\n  rtmps\nOutput:\n  file\n  pipe\n  rtmp\n  rtmps\n"
)
_LGPL_PROTOCOLS = "Supported file protocols:\nInput:\n  file\n  pipe\nOutput:\n  file\n  pipe\n"
_FULL_MUXERS = (
    "File formats:\n"
    " D. = Demuxing supported\n"
    " .E = Muxing supported\n"
    "--\n"
    " E flv             FLV (Flash Video)\n"
    " E s16le           PCM signed 16-bit\n"
)
_LGPL_MUXERS = (
    "File formats:\n"
    " D. = Demuxing supported\n"
    " .E = Muxing supported\n"
    "--\n"
    " E pcm_s16le       PCM signed 16-bit\n"
    " E wav             WAV\n"
    " E null            raw null\n"
)
_FULL_ENCODERS = (
    "Encoders:\n"
    " ------\n"
    " V....D libx264           H.264 / AVC / MPEG-4 AVC\n"
    " A....D aac               AAC (Advanced Audio Coding)\n"
)
_LGPL_ENCODERS = (
    "Encoders:\n"
    " ------\n"
    " A....D pcm_s16le         PCM signed 16-bit\n"
    " A....D pcm_f32le         PCM 32-bit floating point\n"
)


def _probe_listing(flag: str) -> str | None:
    if "-hide_banner" not in sys.argv or flag not in sys.argv:
        return None
    lgpl = os.environ.get("FAKE_FFMPEG_CAPS") == "lgpl"
    if flag == "-protocols":
        return _LGPL_PROTOCOLS if lgpl else _FULL_PROTOCOLS
    if flag == "-muxers":
        return _LGPL_MUXERS if lgpl else _FULL_MUXERS
    if flag == "-encoders":
        return _LGPL_ENCODERS if lgpl else _FULL_ENCODERS
    return None


for _flag in ("-protocols", "-muxers", "-encoders"):
    _listing = _probe_listing(_flag)
    if _listing is not None:
        sys.stdout.write(_listing)
        sys.exit(0)

argv_file = os.environ.get("FAKE_FFMPEG_ARGV_FILE")
if argv_file:
    with open(argv_file, "w", encoding="utf-8") as handle:
        json.dump(sys.argv[1:], handle)

target = sys.argv[-1] if len(sys.argv) > 1 else ""
sys.stderr.write(f"fake-ffmpeg: pushing to {target}\n")
sys.stderr.flush()

die_after = int(os.environ.get("FAKE_FFMPEG_DIE_AFTER_BYTES") or 0)
tee_file = os.environ.get("FAKE_FFMPEG_TEE_FILE")
count_file = os.environ.get("FAKE_FFMPEG_COUNT_FILE")
total = 0

while data := os.read(0, 65536):
    total += len(data)
    if tee_file:
        with open(tee_file, "ab") as handle:
            handle.write(data)
    if die_after and total >= die_after:
        sys.stderr.write(f"fake-ffmpeg: connection to {target} refused\n")
        sys.stderr.flush()
        sys.exit(1)

if count_file:
    with open(count_file, "w", encoding="utf-8") as handle:
        handle.write(str(total))
sys.exit(0)
