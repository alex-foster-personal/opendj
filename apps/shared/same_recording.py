"""Same-recording check before a track's source file is swapped (RECON-07).

A track's source may only be repointed at, or replaced by, a file that is the
same recording. Thu 5 Nov 2026: "Carmen - Opera Dance Music" (Spotify
287.6 s) was ingested as a different, 275.8 s recording and nothing compared
the lengths. A different recording of the same title differs in length far
more often than a re-encode does, so duration is the cheap first gate; a
fingerprint or chroma match can be layered on later without changing callers.

Requirements:
* [if] both durations are known and differ by no more than
  ``SAME_RECORDING_DURATION_TOLERANCE`` of the expected duration [then]
  ``check_same_recording`` returns a verdict with ``same`` True.
* [if] they differ by more than that [then] ``same`` is False and
  ``reason`` names both durations and the drift in percent.
* [if] either duration is missing or not positive [then]
  ``SameRecordingUnverifiable`` is raised: an unknown length never passes.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Largest relative length drift still treated as the same recording. A
#: re-encode or a higher-bitrate copy of one master stays well under 0.1 %;
#: Carmen's wrong recording drifted 4.1 %.
SAME_RECORDING_DURATION_TOLERANCE = 0.01


class SameRecordingUnverifiable(ValueError):
    """Raised when a duration needed for the check is missing."""


@dataclass(frozen=True)
class RecordingVerdict:
    expected_s: float
    candidate_s: float
    drift_ratio: float
    same: bool

    @property
    def reason(self) -> str:
        return (
            f"candidate is {self.candidate_s:.2f} s but the track is "
            f"{self.expected_s:.2f} s ({100 * self.drift_ratio:.2f} % drift, "
            f"limit {100 * SAME_RECORDING_DURATION_TOLERANCE:.0f} %)"
        )


def check_same_recording(expected_s: float | None, candidate_s: float | None) -> RecordingVerdict:
    """Compare the track's recorded length with a candidate file's length."""
    if expected_s is None or expected_s <= 0:
        raise SameRecordingUnverifiable("the track has no recorded duration to compare against")
    if candidate_s is None or candidate_s <= 0:
        raise SameRecordingUnverifiable("the candidate file has no readable duration")
    drift_ratio = abs(candidate_s - expected_s) / expected_s
    return RecordingVerdict(
        expected_s=expected_s,
        candidate_s=candidate_s,
        drift_ratio=drift_ratio,
        same=drift_ratio <= SAME_RECORDING_DURATION_TOLERANCE,
    )


__all__ = [
    "SAME_RECORDING_DURATION_TOLERANCE",
    "RecordingVerdict",
    "SameRecordingUnverifiable",
    "check_same_recording",
]
