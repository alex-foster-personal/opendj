"""Library-shaped PARITY-01 payloads. Real dicts, not mocked APIs.

A payload is the extract the scorer reads: present-or-not, rekordbox ground
truth per lane, and our own analysis when it exists. Tests build these by
hand so a figure's denominator is a count the test can name, never a hidden
default.
"""

from __future__ import annotations

from typing import Any

MEASURED_AT = "Fri 11 Sep 2026"

# The two library-wide totals PARITY-01 forbids as a figure's denominator.
# They include roughly 7800 rows with no audio to analyze.
TRACKS_ROWS_TOTAL = 9986
DJMD_CONTENT_LIVE_TOTAL = 10479


def track(
    stable_id: str,
    *,
    present: bool = True,
    rb_bpm_x100: int | None = 12800,
    own_bpm: float | None = 128.0,
    rb_key_scale_name: str | None = "Am",
    own_key_camelot: str | None = "8A",
    own_key_openkey: str | None = "1m",
    rb_pssi: bool = False,
    rb_cue_db: bool = False,
    rb_ext_readable: bool = True,
    rb_pvdi: bool = False,
) -> dict[str, Any]:
    """One library-shaped row. Absent fields stay explicit None, never defaulted later."""
    return {
        "stable_id": stable_id,
        "present": present,
        "rb_bpm_x100": rb_bpm_x100,
        "own_bpm": own_bpm,
        "rb_key_scale_name": rb_key_scale_name,
        "own_key_camelot": own_key_camelot,
        "own_key_openkey": own_key_openkey,
        "rb_pssi": rb_pssi,
        "rb_cue_db": rb_cue_db,
        "rb_ext_readable": rb_ext_readable,
        "rb_pvdi": rb_pvdi,
    }


def payload(tracks: list[dict[str, Any]], *, measured_at: str = MEASURED_AT) -> dict[str, Any]:
    """A scorable extract. Population totals are recorded so a scorer that
    divides by them can be caught; they are not the lane denominators.
    """
    return {
        "schema": 1,
        "measured_at": measured_at,
        "population": {
            "present": sum(1 for row in tracks if row["present"]),
            "tracks_rows": TRACKS_ROWS_TOTAL,
            "djmd_content_live": DJMD_CONTENT_LIVE_TOTAL,
        },
        "tracks": tracks,
    }


def round0_fixture() -> dict[str, Any]:
    """Eight present tracks covering the first-round BPM and Key cases.

    Named so the round-0 log can point at a fixture the tests also score.
    """
    return payload(
        [
            track("exact-128"),
            track(
                "exact-key-c",
                rb_key_scale_name="C",
                own_key_camelot="8B",
                own_key_openkey="1d",
            ),
            track("bpm-within-0.1", own_bpm=128.08),
            track("bpm-octave-half", own_bpm=64.0),
            track(
                "key-relative",
                rb_key_scale_name="C",
                own_key_camelot="8A",
                own_key_openkey="1m",
            ),
            track("missing-rb-bpm", rb_bpm_x100=None),
            track("missing-rb-key", rb_key_scale_name=None),
            track("no-own-bpm", own_bpm=None),
            track("no-own-key", own_key_camelot=None, own_key_openkey=None),
            track("ungradable-key-all", rb_key_scale_name="All"),
            track("absent-audio", present=False),
        ]
    )
