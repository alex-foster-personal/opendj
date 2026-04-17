"""Reader smoke test against the big (1586-track) LaCie-hosted fixture.

Covers CAT-06 at full library scale. The small in-repo fixture has 199
tracks — enough to exercise every code path, but not enough to catch
issues that only appear at real-library size (pagination, string
encoding edge cases, playlist-tree depth). This test parses the full
export and asserts we're in the right ballpark.

Marked ``slow`` because the parse takes several seconds on the full
fixture; uses the ``big_usb_fixture`` pytest fixture so it skips
cleanly when LaCie isn't mounted.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.sync.usb.pioneer import read_usb_export
from apps.sync.usb.pioneer.reader import validate_invariants


pytestmark = [
    pytest.mark.requirement("CAT-06"),
    pytest.mark.slow,
]


@pytest.fixture(scope="module")
def big_usb_data(big_usb_fixture: Path) -> dict:
    """Parse the big fixture once per module (≈5s on macOS)."""
    pioneer = big_usb_fixture / "PIONEER"
    assert pioneer.is_dir(), f"expected PIONEER/ under {big_usb_fixture}"
    return read_usb_export(pioneer)


def test_big_fixture_track_count(big_usb_data: dict) -> None:
    """At least 1500 tracks (real export ≈1586)."""
    total = big_usb_data["metadata"]["total_tracks"]
    assert total >= 1500, f"expected ≥1500 tracks, got {total}"


def test_big_fixture_has_playlists(big_usb_data: dict) -> None:
    """Playlists populated (real export has many — at least 20)."""
    total = big_usb_data["metadata"]["total_playlists"]
    assert total >= 20, f"expected ≥20 playlists, got {total}"


def test_big_fixture_has_onelibrary(big_usb_data: dict) -> None:
    """Export has the SQLCipher OneLibrary sidecar."""
    assert big_usb_data["metadata"]["has_onelibrary"] is True


def test_big_fixture_has_extended_pdb(big_usb_data: dict) -> None:
    """Export has ``exportExt.pdb`` (Rekordbox 6.6+ extended metadata)."""
    assert big_usb_data["metadata"]["has_extended_pdb"] is True


def test_big_fixture_anlz_coverage(big_usb_data: dict) -> None:
    """ANLZ dirs cover every track (parity with total_tracks)."""
    meta = big_usb_data["metadata"]
    # On a real export the counts are usually identical; we allow a tiny
    # slack for tracks that Rekordbox never analysed.
    assert meta["anlz_total_dirs"] >= meta["total_tracks"] * 0.95, (
        f"ANLZ coverage {meta['anlz_total_dirs']} << "
        f"tracks {meta['total_tracks']}"
    )


def test_big_fixture_invariants_pass(big_usb_data: dict) -> None:
    """Reader-level invariants (playlist-entry FK, bpm range, …) pass.

    NOTE: real exports may legitimately fail the "ANLZ count == track
    count" invariant — Rekordbox often has a handful of tracks still
    pending analysis at export time. This test allows a small ANLZ-gap
    (≤1% of library) but enforces every other invariant strictly.
    """
    errors = validate_invariants(big_usb_data)
    meta = big_usb_data["metadata"]
    anlz_gap_tolerance = max(5, int(meta["total_tracks"] * 0.01))

    def _is_anlz_gap(msg: str) -> bool:
        return msg.startswith("ANLZ directory count") and "< track count" in msg

    strict_errors: list[str] = []
    for err in errors:
        if _is_anlz_gap(err):
            gap = meta["total_tracks"] - meta["anlz_total_dirs"]
            if gap <= anlz_gap_tolerance:
                continue  # tolerated
        strict_errors.append(err)

    assert strict_errors == [], (
        f"{len(strict_errors)} invariant failures (tolerated "
        f"{len(errors) - len(strict_errors)}): {strict_errors[:5]}"
    )
