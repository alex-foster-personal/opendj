"""Vocal worker launch config and preflight (split from ``apps.vocals.cli``).

Requirements (mini-PRD):
  ✔︎ ✅ Worker preflight (INSTALL-27): run_worker refuses BEFORE launching
    the worker when the launch could only fail, so the operator reads a
    recovery step instead of "vocal_region_worker failed (exit N)".
    [if] MDT_VOCAL_WORKER_SCRIPT names a missing or unreadable file [then ⛔️]
    reinstall-from-a-complete-dmg guidance, no worker spawned
    [if] input is not .wav and no ffmpeg is reachable (MDT_FFMPEG or PATH,
    the worker's own lookup) and no odj-audio either (ODJ_AUDIO_BIN, else a
    local cargo build) [then ⛔️] a decoder message naming the file and the
    remedies, no worker spawned
    [if] input is not .wav, no ffmpeg, odj-audio resolves [then] the worker
    is launched and decodes through ``odj-audio decode`` (STEM-50)

-Claude
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

# ----- CFG ---------------------------------------------------------------------
WORKER_TIMEOUT_S: float = 30 * 60
# Per rung: the polite signal gets this long, and so does the forced rung's confirm.
WORKER_TERMINATE_GRACE_S: float = 2
WORKER_SCRIPT: Path = Path(__file__).resolve().parents[2] / "scripts" / "vocal_region_worker.py"
PACKAGED_WORKER_SCRIPT_ENV: str = "MDT_VOCAL_WORKER_SCRIPT"
PACKAGED_WORKER_PYTHON_ENV: str = "MDT_VOCAL_WORKER_PYTHON"
# Same variable scripts/vocal_region_worker.py::_read_via_ffmpeg reads.
FFMPEG_OVERRIDE_ENV: str = "MDT_FFMPEG"
WORKER_UNAVAILABLE_MESSAGE: str = (
    "Vocal separation is unavailable because the bundled audio runtime "
    "could not be started. Reinstall Open DJ from a complete dmg and "
    "try again."
)


class WorkerUnavailableError(RuntimeError):
    """The worker cannot run this input here; raised before any launch."""


def _ffmpeg_reachable(environ: Mapping[str, str]) -> bool:
    """Mirror the worker's decoder lookup: MDT_FFMPEG's dir first, then PATH."""
    search = environ.get("PATH", os.defpath)
    override = environ.get(FFMPEG_OVERRIDE_ENV)
    if override:
        search = os.pathsep.join([str(Path(override).parent), search])
    return shutil.which("ffmpeg", path=search) is not None


def preflight_worker(audio_path: Path, environ: Mapping[str, str]) -> None:
    """Refuse a worker launch that can only fail, with a recovery message.

    Two launches are knowable failures before the worker imports torch:
    a packaged worker script that is not there (an incomplete install),
    and a non-WAV input with neither ffmpeg nor odj-audio to decode it (the
    worker decodes only ``.wav`` itself, via soundfile). ``environ`` is the environment
    the worker would inherit.
    """
    packaged_script = environ.get(PACKAGED_WORKER_SCRIPT_ENV)
    if packaged_script:
        script = Path(packaged_script)
        if not (script.is_file() and os.access(script, os.R_OK)):
            raise WorkerUnavailableError(WORKER_UNAVAILABLE_MESSAGE)
    if audio_path.suffix.lower() != ".wav" and not _ffmpeg_reachable(environ):
        # The installed app ships no ffmpeg; the worker decodes through
        # odj-audio instead, which the launcher names in ODJ_AUDIO_BIN.
        from apps.shared.odj_audio_decode import NoDecoderError, resolve_odj_audio

        try:
            resolve_odj_audio(audio_path, environ)
        except NoDecoderError as exc:
            raise WorkerUnavailableError(
                f"Vocal separation cannot read {audio_path.name}: only WAV files "
                "can be decoded without ffmpeg or the odj-audio engine, and "
                f"neither was found ({exc}). Install ffmpeg, set "
                f"{FFMPEG_OVERRIDE_ENV} to its path, set ODJ_AUDIO_BIN to the "
                "odj-audio engine, or convert the track to WAV, then try again."
            ) from None  # its text is in the message; no cause means "never launched"
