"""Decode audio with the app's own Rust engine (``odj-audio``), not ffmpeg.

Mini-PRD
--------
R1 ok   Resolve ``odj-audio`` as the engine supervisor does: ``ODJ_AUDIO_BIN``
        (set by every packaged payload's launcher) wins and is never
        second-guessed; without it, the newest local cargo build in this
        checkout.
R2 ok   :func:`probe_duration_s` reports the playable length the container
        states (``odj-audio probe``), or a full decode's frame count when it
        states none; ``None`` when the engine cannot read the file. Never an
        estimate from bitrate.
R3 ok   Fail loudly on the host: an absent or unusable binary raises
        :class:`EngineDecoderUnavailable`; it never reads as a fact about a file.

Acceptance
----------
[if] ODJ_AUDIO_BIN names an executable file  [then] it is used, no repo build read
[if] ODJ_AUDIO_BIN is set but not executable [then] EngineDecoderUnavailable names it
[if] ODJ_AUDIO_BIN is unset                  [then] the newest repo release/debug build
[if] no binary at all                        [then] EngineDecoderUnavailable
[if] a binary predates decode/probe          [then] it is passed over (repo) or refused (env)
[if] the file is not audio                   [then] probe_duration_s returns None

Why: the shipped app bundles ``odj-audio`` (``scripts/build_engine_payload.py``
stages ``bin/odj-audio`` and exports ``ODJ_AUDIO_BIN``) but no ffmpeg, so the
analysis lanes that shell out to ffmpeg cannot run on an installed app unless
the user has installed ffmpeg themselves. The engine decodes with symphonia
(MPL-2.0), in-process, with the MP4 edit list applied; measured against
ffmpeg 6.1 on Thu 1 Oct 2026 it matched sample-for-sample in time on WAV,
AIFF, FLAC, ALAC, MP3, Vorbis and AAC m4a. See
``research/audio-decode/2026-10-01-packaging-audio-decode.md``.

The resolver mirrors ``apps.engine_core.audio_engine.resolve_binary`` rather
than importing it, because ``apps.shared`` is the stable core and may not
import ``apps.engine_core`` (``.importlinter``).
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from apps.shared.paths import PROJECT_ROOT

BIN_ENV = "ODJ_AUDIO_BIN"
"""Set by the packaged payload's launcher to its bundled ``bin/odj-audio``."""

EXE_NAME = "odj-audio.exe" if os.name == "nt" else "odj-audio"
REPO_TARGET = Path("apps/audio-engine/target")
PROBE_TIMEOUT_S = 120
"""A header read is instant; the bound covers a full-decode count of a long
MP3 with no Xing header (about 0.6 s per 6 minutes, measured in the 20-05
null test), on a contended host."""


#: Subcommands this module calls. A build older than them prints its usage
#: and exits 2 for each, which would read as "every file is unreadable".
REQUIRED_COMMANDS: tuple[str, ...] = ("decode", "probe")


class EngineDecoderUnavailable(RuntimeError):
    """``odj-audio`` cannot be located or executed. Never a degraded result."""


@functools.lru_cache(maxsize=16)
def _has_commands(path: str, size: int, mtime_ns: int) -> bool:
    """Whether the binary's usage lists every required subcommand.

    Asked of the binary itself, because a reused CI workspace or an old local
    cargo build can hold an ``odj-audio`` that predates them (seen on the
    nucbox runner, Thu 1 Oct 2026). Cached by path, size and mtime, so a
    rebuilt binary is asked again.
    """
    del size, mtime_ns  # cache key only
    try:
        done = subprocess.run(  # fixed argv, never a shell
            [path, "help"], capture_output=True, text=True, errors="replace",
            check=False, stdin=subprocess.DEVNULL, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and all(
        f"odj-audio {cmd} " in done.stdout for cmd in REQUIRED_COMMANDS
    )


def _supports(p: Path) -> bool:
    st = p.stat()
    return _has_commands(str(p), st.st_size, st.st_mtime_ns)


def resolve_engine_decoder(
    environ: Mapping[str, str] | None = None, repo_root: Path = PROJECT_ROOT
) -> Path:
    """Path to ``odj-audio``: ``ODJ_AUDIO_BIN``, else the newest repo build."""
    env = os.environ if environ is None else environ
    raw = env.get(BIN_ENV, "").strip()
    if raw:
        p = Path(raw)
        if not (p.is_file() and os.access(p, os.X_OK)):
            raise EngineDecoderUnavailable(f"{BIN_ENV}={raw!r} is not an executable file")
        if not _supports(p):
            raise EngineDecoderUnavailable(
                f"{BIN_ENV}={raw!r} predates the {'/'.join(REQUIRED_COMMANDS)} subcommands"
            )
        return p
    found = [
        p
        for p in (repo_root / REPO_TARGET / profile / EXE_NAME for profile in ("release", "debug"))
        if p.is_file() and os.access(p, os.X_OK) and _supports(p)
    ]
    if not found:
        raise EngineDecoderUnavailable(
            f"no {BIN_ENV} and no local odj-audio build with "
            f"{'/'.join(REQUIRED_COMMANDS)}; run "
            "`cargo build --release --manifest-path apps/audio-engine/Cargo.toml`"
        )
    return max(found, key=lambda p: p.stat().st_mtime)


def probe_duration_s(path: Path, exe: Path | None = None) -> float | None:
    """Seconds of playable audio in ``path``, or ``None`` if the engine cannot read it."""
    binary = exe or resolve_engine_decoder()
    try:
        done = subprocess.run(  # fixed argv, never a shell
            [str(binary), "probe", str(path)],
            capture_output=True, text=True, errors="replace", check=False,
            stdin=subprocess.DEVNULL, timeout=PROBE_TIMEOUT_S,
        )
    except OSError as exc:
        raise EngineDecoderUnavailable(f"{binary} could not be launched: {exc}") from None
    except subprocess.TimeoutExpired:
        # The length is unknown THIS time; admission refuses it by name and a
        # later pass asks again.
        return None
    if done.returncode != 0:
        return None
    try:
        seconds = float(json.loads(done.stdout)["duration_s"])
    except (ValueError, KeyError, TypeError):
        return None
    return seconds if seconds > 0 else None


__all__ = [
    "BIN_ENV",
    "EngineDecoderUnavailable",
    "probe_duration_s",
    "resolve_engine_decoder",
]
