"""RECON-07: the same-recording length gate.

[if] a candidate's length drifts more than 1 % from the track's, or either length is unknown [then] it is not the same recording, [else stop].
"""

from __future__ import annotations

import pytest

from apps.shared.same_recording import (
    SAME_RECORDING_DURATION_TOLERANCE,
    SameRecordingUnverifiable,
    check_same_recording,
)

pytestmark = pytest.mark.requirement("RECON-07")


def test_carmen_wrong_recording_is_refused() -> None:
    # Spotify release 287.640 s; the file ingested Mon 26 Oct was 275.826 s.
    verdict = check_same_recording(287.640, 275.826)
    assert not verdict.same, (
        "if Carmen's 4.1 % shorter recording passes then the gate misses the incident it exists for"
    )
    assert "275.83 s" in verdict.reason and "287.64 s" in verdict.reason


def test_higher_bitrate_copy_of_same_master_passes() -> None:
    # Y do I: 128 kbps import 472.607 s, 264 kbps copy 472.576 s.
    assert check_same_recording(472.607, 472.576).same, (
        "if a re-encode of one master is refused then upgrades are blocked"
    )


def test_boundary_is_inclusive() -> None:
    edge = 100.0 * (1 + SAME_RECORDING_DURATION_TOLERANCE)
    assert check_same_recording(100.0, edge).same
    assert not check_same_recording(100.0, edge + 0.01).same


@pytest.mark.parametrize(("expected", "candidate"), [(None, 200.0), (0.0, 200.0), (200.0, None), (200.0, -1.0)])
def test_unknown_length_never_passes(expected: float | None, candidate: float | None) -> None:
    with pytest.raises(SameRecordingUnverifiable):
        check_same_recording(expected, candidate)
