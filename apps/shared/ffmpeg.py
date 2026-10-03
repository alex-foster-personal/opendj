"""Locate the ffmpeg executable this repo decodes audio with, in one place.

Mini-PRD
--------
R1 ok   Resolve the ffmpeg binary: ``MDT_FFMPEG`` first, then the payload's
        bundled build (``ODJ_FFMPEG_BIN``, exported by the payload launcher the
        same way ``ODJ_AUDIO_BIN`` locates ``bin/odj-audio``), then PATH.
        PATH is never preferred over the bundled build.
R2 ok   Fail loudly: an absent binary, or a set-but-unusable ``MDT_FFMPEG``,
        raises :class:`FfmpegUnavailable`. There is no second decoder to fall
        back to and no silent PATH lookup behind a broken override.
R3 ok   :func:`probe_duration_s` reports the length ffmpeg's demuxer states for
        a file, or ``None`` when it states none; never an estimate of ours
        (NATIVE-10: payloads built before the tag reader shipped carried no
        duration and the backfill queue asks the decoder instead).

Acceptance
----------
[if] MDT_FFMPEG names an executable file       [then] it is returned, PATH unread
[if] MDT_FFMPEG is a relative path             [then] an ABSOLUTE path is returned
[if] MDT_FFMPEG is set but not executable      [then] FfmpegUnavailable names MDT_FFMPEG
[if] MDT_FFMPEG is unset and ODJ_FFMPEG_BIN is executable [then] the bundled one wins over PATH
[if] ODJ_FFMPEG_BIN is set but not executable  [then] FfmpegUnavailable names it, PATH unread
[if] both are unset and ffmpeg is on PATH      [then] the PATH hit is returned
[if] neither is available                      [then] FfmpegUnavailable names the override

Why this is shared rather than private to one module: ffmpeg is the only
audio decoder the web server actually has, so more than one caller needs the
same answer to "where is ffmpeg". Before this module existed,
``apps/analysis/pcm_fingerprint.py`` reached into
``apps/analysis_waveform/decode.py`` for the lookup alone and caught that
module's ``LocalDecodeUnavailable`` for a failure that has nothing to do with
waveform decoding. One resolver means one answer, one home for the
packaged-app PATH escape hatch that motivated ``MDT_FFMPEG``
(discussion_r3908337225, issue #735 follow-up), and no module importing a
decoder it does not use.

Deliberately NOT migrated: ``scripts/stem_bundle_worker.py`` and
``scripts/vocal_region_worker.py`` read ``MDT_FFMPEG`` with different
semantics on purpose (a bare-name fallback handed to ffmpeg, and a
``PATH`` mutation respectively). Routing them here would be a behaviour
change wearing a de-duplication's clothes, so they are left alone.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

FFMPEG_BINARY = "ffmpeg"
"""PATH name looked up when neither override is set."""

OVERRIDE_ENV = "MDT_FFMPEG"
"""Operator override; always wins."""
BUNDLED_ENV = "ODJ_FFMPEG_BIN"
"""The payload's own LGPL build (``payload/bin/ffmpeg``), set by the launcher."""


class FfmpegUnavailable(RuntimeError):
    """ffmpeg cannot be located or executed. Never a degraded result."""


def resolve_ffmpeg() -> str:
    """Path to the ffmpeg executable: ``MDT_FFMPEG``, else bundled, else PATH.

    A packaged/GUI-launched app does not inherit Homebrew's PATH the way a
    shell does, so a bare PATH lookup can find nothing even with ffmpeg
    installed. ``MDT_FFMPEG`` is the same escape hatch
    ``scripts/vocal_region_worker.py`` and ``scripts/stem_bundle_worker.py``
    already use for this.

    A set-but-broken ``MDT_FFMPEG`` raises rather than falling back to PATH:
    silently ignoring an explicit override would mask the misconfiguration
    (fail-fast, no hidden defaults). Nothing here mutates ``os.environ`` --
    one caller is a long-lived multi-threaded server, where a process-wide
    PATH mutation on a request path would race every concurrent caller.
    """
    for env_name in (OVERRIDE_ENV, BUNDLED_ENV):
        configured = os.environ.get(env_name)
        if configured:
            return _executable_or_raise(env_name, configured)
    exe = shutil.which(FFMPEG_BINARY)
    if exe is None:
        raise FfmpegUnavailable(
            "ffmpeg is not on PATH, so no audio can be decoded "
            f"(set {OVERRIDE_ENV} to an ffmpeg executable path to override - a "
            "packaged app launch does not inherit Homebrew's PATH)"
        )
    return exe


def _executable_or_raise(env_name: str, configured: str) -> str:
    """``configured`` as an absolute executable path, or raise naming ``env_name``.

    Absolute, because subprocess hands a bare basename to a PATH search. A
    relative override such as "customff" that sits in the working directory
    passes the checks below and is then either not found at all or resolved
    to a DIFFERENT binary on PATH, which is the one failure mode an explicit
    override exists to rule out. abspath rather than resolve: this fixes the
    lookup, it does not silently follow a symlink the operator deliberately
    pointed at. A set-but-broken bundled path raises too: a packaged app that
    lost its own ffmpeg must say so, not quietly decode with whatever PATH has.
    """
    resolved = os.path.abspath(configured)
    if Path(resolved).is_file() and os.access(resolved, os.X_OK):
        return resolved
    raise FfmpegUnavailable(f"{env_name}={configured!r} is not an executable file")


#: Where Homebrew installs ffmpeg (Apple silicon, then Intel). A Finder-launched
#: app inherits launchd's PATH (/usr/bin:/bin:/usr/sbin:/sbin), which holds
#: neither, so a Mac with ffmpeg installed still fails the PATH lookup.
HOMEBREW_FFMPEG_PATHS: tuple[str, ...] = ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")


def resolve_ffmpeg_including_homebrew() -> str:
    """:func:`resolve_ffmpeg`, then Homebrew's prefixes when nothing overrides.

    For callers that run inside the packaged app (set recording, SET-10). A
    set-but-broken ``MDT_FFMPEG`` still raises rather than falling through.
    """
    try:
        return resolve_ffmpeg()
    except FfmpegUnavailable:
        if os.environ.get(OVERRIDE_ENV) or os.environ.get(BUNDLED_ENV):
            raise  # a set-but-broken override or bundled path never falls through
        for candidate in HOMEBREW_FFMPEG_PATHS:
            if Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return candidate
        raise FfmpegUnavailable(
            "ffmpeg was not found on PATH or in "
            f"{', '.join(HOMEBREW_FFMPEG_PATHS)} (install it with `brew install "
            "ffmpeg`, or set MDT_FFMPEG)"
        ) from None


#: ``Duration: HH:MM:SS.ss`` in ffmpeg's input report; ``N/A`` does not match.
_DURATION_LINE = re.compile(r"^\s*Duration: (\d+):(\d{2}):(\d{2}(?:\.\d+)?),", re.MULTILINE)
PROBE_TIMEOUT_S = 30


def probe_duration_s(path: Path) -> float | None:
    """Seconds ffmpeg's demuxer reports for ``path``, or ``None`` if it reports none.

    Reads the container header only (``ffmpeg -i`` with no output exits 1 by
    design after printing the input report), so it costs a process spawn, not
    a decode. ``None`` covers a file ffmpeg cannot open and one whose length
    is ``N/A``; a missing ffmpeg raises :class:`FfmpegUnavailable` instead,
    because that is a host fault, not a fact about the file.
    """
    report = subprocess.run(
        [resolve_ffmpeg(), "-hide_banner", "-nostdin", "-i", str(path)],
        capture_output=True, text=True, errors="replace", check=False,
        timeout=PROBE_TIMEOUT_S,
    ).stderr
    match = _DURATION_LINE.search(report)
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return total if total > 0 else None


__all__ = [
    "BUNDLED_ENV",
    "FFMPEG_BINARY",
    "OVERRIDE_ENV",
    "HOMEBREW_FFMPEG_PATHS",
    "FfmpegUnavailable",
    "probe_duration_s",
    "resolve_ffmpeg",
    "resolve_ffmpeg_including_homebrew",
]
