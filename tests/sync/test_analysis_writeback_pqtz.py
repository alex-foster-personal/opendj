"""Fixed-tempo PQTZ bytes encode/decode for own artifacts (issue #11140).

[if] own-grid PQTZ bytes are written and read [then] served beats match quantize rules, [else stop].
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.sync.analysis_writeback_pqtz import (
    build_minimal_dat_bytes,
    quantize_own_beat,
    quantize_time_to_served_beat,
    refuse_dynamic_tempo_payload,
    served_beats_from_own,
    served_beats_from_pqtz_bytes,
)

pytestmark = pytest.mark.requirement("BEATGRID-13512")


def round_trip_via_own(beats: list[dict[str, Any]]) -> list[dict[str, float | int]]:
    return served_beats_from_pqtz_bytes(build_minimal_dat_bytes(beats))


SERVED = [{"n": (i % 4) + 1, "bpm": 125.0, "t": round(0.48 * i, 3)} for i in range(16)]


def test_fixed_tempo_bytes_round_trip_matches_served_beats() -> None:
    """[if] a fixed-tempo grid is encoded then parsed [then] wire beats match quantize-then-read, [else stop]."""
    assert round_trip_via_own(SERVED) == served_beats_from_own(SERVED)


def test_non_empty_tempo_changes_are_refused_for_encoding() -> None:
    """[if] a payload has non-empty tempo_changes [then] encoding is refused, [else stop]."""
    with pytest.raises(ValueError, match="tempo_changes"):
        refuse_dynamic_tempo_payload({"beats": SERVED, "tempo_changes": [{"at_s": 1.0}]})


def test_beat_n_outside_one_to_four_is_refused() -> None:
    """[if] a beat n outside 1..4 is offered [then] quantize refuses it, [else stop]."""
    with pytest.raises(ValueError, match="1..4"):
        quantize_own_beat({"n": 5, "bpm": 120.0, "t": 1.0})


def test_quantize_snaps_a_seek_time_to_the_nearest_read_back_beat() -> None:
    """[if] quantize runs on read-back beats [then] the snap lands on a served t, [else stop]."""
    read = round_trip_via_own(SERVED)
    snapped = quantize_time_to_served_beat(read, 1.05)
    assert snapped in {b["t"] for b in read}
    assert snapped == pytest.approx(0.96, abs=0.05)
