"""EBU R128 loudness scan for one audio file, via the ffmpeg ``ebur128`` filter.

Mini-PRD
--------
R1 ok   Report integrated loudness (LUFS), loudness range (LU) and true peak
        (dBTP) for a single file, in one ffmpeg pass.
R2 ok   Derive a ReplayGain-style gain to a caller-supplied target, clamped so
        applying it cannot push true peak above a caller-supplied ceiling.
R3 ok   Fail loudly: a missing ffmpeg, an ffmpeg that lacks the filter, a
        non-zero exit, or a summary that does not parse are all hard errors.
        There is no fallback path and no partial result.
R4 to-do Persist scans into state.db so the UI can read per-track gain.

Acceptance
----------
[if] ffmpeg is absent from PATH and MDT_FFMPEG  [then] LoudnessError names it
[if] the file does not exist                    [then] LoudnessError, no ffmpeg call
[if] ffmpeg exits non-zero                      [then] LoudnessError carries stderr
[if] the summary block is missing a field       [then] LoudnessError names the field
[if] a -6 dBFS sine is scanned                  [then] true_peak_dbtp is within 0.5 of -6
[if] gain would drive true peak over ceiling    [then] gain is clamped, clamped=True

Why ffmpeg as a subprocess and not a library: an arm's-length CLI invocation is
not linking, so this stays clean under Apache-2.0 regardless of how the local
ffmpeg was built. See docs/research/adrian-level-meters-clipping-lights.md.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from apps.shared.ffmpeg import FfmpegUnavailable, resolve_ffmpeg

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

# EBU R128 broadcast target. Callers that want the streaming convention pass
# -14.0 explicitly; nothing here guesses on their behalf.
DEFAULT_TARGET_LUFS = -23.0
# Headroom left below full scale after gain is applied. -1.0 dBTP is the
# mastering convention, chosen because lossy codecs overshoot on decode.
DEFAULT_CEILING_DBTP = -1.0
SCAN_TIMEOUT_S = 900


class LoudnessError(RuntimeError):
    """Raised for every failure mode. There is no degraded result."""


@dataclass(frozen=True)
class LoudnessScan:
    """One file's measured loudness. Every field is measured, none inferred."""

    path: Path
    integrated_lufs: float
    loudness_range_lu: float
    true_peak_dbtp: float

    def gain_db(
        self,
        target_lufs: float = DEFAULT_TARGET_LUFS,
        ceiling_dbtp: float = DEFAULT_CEILING_DBTP,
    ) -> tuple[float, bool]:
        """Gain to reach ``target_lufs``, clamped to keep peaks under the ceiling.

        Returns (gain_db, clamped). ``clamped`` is True when peak headroom, not
        the loudness target, decided the number: the caller needs to know the
        track will still land quiet.
        """
        wanted = target_lufs - self.integrated_lufs
        headroom = ceiling_dbtp - self.true_peak_dbtp
        if wanted <= headroom:
            return (wanted, False)
        return (headroom, True)


# --------------------------------------------------------------------------
# ffmpeg summary parsing
# --------------------------------------------------------------------------

# The ebur128 summary is a fixed indented block on stderr. Each field is
# anchored to its own label so a reordered or extended block cannot silently
# feed the wrong number into the wrong slot.
_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "integrated_lufs": re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS\s*$", re.M),
    "loudness_range_lu": re.compile(r"^\s*LRA:\s*(-?\d+(?:\.\d+)?)\s*LU\s*$", re.M),
    # With ``ebur128=peak=true`` ffmpeg's "True peak" summary section emits
    # true peak, despite labelling its numeric unit dBFS. The numeric dBFS
    # value is the dBTP value for this full-scale reference. Running TPK and
    # the final summary must agree; tests cover an inter-sample overshoot.
    "true_peak_dbtp": re.compile(r"^\s*Peak:\s*(-?\d+(?:\.\d+)?|-inf)\s*dBFS\s*$", re.M),
}


def _parse_summary(stderr: str, path: Path) -> dict[str, float]:
    """Pull the three summary fields, or raise naming the one that is missing."""
    found: dict[str, float] = {}
    for field, pattern in _FIELD_PATTERNS.items():
        matches = pattern.findall(stderr)
        if not matches:
            raise LoudnessError(
                f"ebur128 summary for {path} has no '{field}' line. "
                f"ffmpeg stderr tail:\n{stderr[-2000:]}"
            )
        # The filter prints running values during the pass and the summary last,
        # so the final match is the summary figure.
        raw = matches[-1]
        if raw == "-inf":
            # Digital silence. Real, not an error, but it must not become 0.0.
            found[field] = float("-inf")
        else:
            found[field] = float(raw)
    return found


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------


def require_ffmpeg() -> str:
    """Resolve the ffmpeg binary, or raise. The one resolution path every
    caller in this lane shares, so executable selection cannot drift between
    the measurements that need it (specs/native-analysis-v1.md:601).

    Delegates to :func:`apps.shared.ffmpeg.resolve_ffmpeg` so this lane honors
    ``MDT_FFMPEG`` like every other decoder caller: an installed app has no
    Homebrew PATH, and a bare PATH lookup here stopped every packaged loudness
    backfill while the other lanes decoded the same files (NATIVE-10)."""
    try:
        return resolve_ffmpeg()
    except FfmpegUnavailable as exc:
        raise LoudnessError(
            f"{exc}. apps.loudness measures with the ffmpeg ebur128 filter and "
            "has no fallback measurement path."
        ) from exc


def scan_file(path: Path, binary: str | None = None) -> LoudnessScan:
    """Measure one file. Raises LoudnessError on any failure.

    ``binary`` lets a caller that needs more than one ffmpeg pass over the
    same file (e.g. :mod:`apps.analysis_loudness.adapter`) resolve once and
    share the result, so a PATH change between passes cannot combine
    measurements from two different ffmpeg binaries. Defaults to resolving
    here, for every caller that only needs the one pass.
    """
    if not path.is_file():
        raise LoudnessError(f"not a file: {path}")
    binary = binary or require_ffmpeg()
    completed = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-map",
            "a:0",
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=SCAN_TIMEOUT_S,
        # Explicit: the return code is inspected below and turned into a
        # LoudnessError carrying stderr, which is more use than CalledProcessError.
        check=False,
    )
    if completed.returncode != 0:
        raise LoudnessError(
            f"ffmpeg exited {completed.returncode} scanning {path}:\n"
            f"{completed.stderr[-2000:]}"
        )
    fields = _parse_summary(completed.stderr, path)
    return LoudnessScan(
        path=path,
        integrated_lufs=fields["integrated_lufs"],
        loudness_range_lu=fields["loudness_range_lu"],
        true_peak_dbtp=fields["true_peak_dbtp"],
    )
