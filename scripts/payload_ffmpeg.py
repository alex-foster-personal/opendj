"""Stage the payload's pinned decode-only LGPL ffmpeg.

Kept out of scripts/build_engine_payload.py so that module stays within the
file-size ratchet. The launcher still exports ODJ_FFMPEG_BIN itself; tests
read that string from ENGINE_LAUNCHER_TEMPLATE.
"""

from __future__ import annotations

from pathlib import Path

FFMPEG_RELATIVE: str = "bin/ffmpeg"


def stage_ffmpeg(payload_dir: Path) -> dict[str, str]:
    """Stage the pinned LGPL ffmpeg at ``bin/ffmpeg`` (build_ffmpeg_lgpl.stage)."""
    from scripts import build_ffmpeg_lgpl
    from scripts.build_engine_payload import PayloadBuildError

    try:
        return build_ffmpeg_lgpl.stage(payload_dir, FFMPEG_RELATIVE)
    except build_ffmpeg_lgpl.FfmpegBuildError as exc:
        raise PayloadBuildError(str(exc)) from exc
