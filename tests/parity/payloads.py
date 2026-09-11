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
    rb_vocal_regions: list[dict[str, float]] | None = None,
    own_vocal_regions: list[dict[str, float]] | None = None,
    duration_s: float | None = None,
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
        "rb_vocal_regions": rb_vocal_regions,
        "own_vocal_regions": own_vocal_regions,
        "duration_s": duration_s,
    }


def payload(
    tracks: list[dict[str, Any]],
    *,
    measured_at: str = MEASURED_AT,
    parity_round: int = 1,
) -> dict[str, Any]:
    """A scorable extract. Population totals are recorded so a scorer that
    divides by them can be caught; they are not the lane denominators.
    """
    return {
        "schema": 1,
        "measured_at": measured_at,
        "parity_round": parity_round,
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
        ],
        parity_round=0,
    )


def round1_fixture() -> dict[str, Any]:
    """Round 1 adds vocal PVDI cases on the same BPM/Key rows as round 0."""
    return payload(
        [
            track(
                "exact-128",
                rb_pvdi=True,
                rb_vocal_regions=[{"start_s": 10.0, "end_s": 90.0}],
                own_vocal_regions=[{"start_s": 10.5, "end_s": 89.5}],
                duration_s=100.0,
            ),
            track(
                "exact-key-c",
                rb_key_scale_name="C",
                own_key_camelot="8B",
                own_key_openkey="1d",
                rb_pvdi=True,
                rb_vocal_regions=[],
                own_vocal_regions=[],
                duration_s=100.0,
            ),
            track(
                "bpm-within-0.1",
                own_bpm=128.08,
                rb_pvdi=True,
                rb_vocal_regions=[{"start_s": 0.0, "end_s": 30.0}],
                own_vocal_regions=[{"start_s": 70.0, "end_s": 100.0}],
                duration_s=100.0,
            ),
            track(
                "bpm-octave-half",
                own_bpm=64.0,
                rb_pvdi=True,
                rb_vocal_regions=[],
                own_vocal_regions=[{"start_s": 20.0, "end_s": 80.0}],
                duration_s=100.0,
            ),
            track(
                "key-relative",
                rb_key_scale_name="C",
                own_key_camelot="8A",
                own_key_openkey="1m",
                rb_pvdi=True,
                rb_vocal_regions=[{"start_s": 5.0, "end_s": 40.0}],
                own_vocal_regions=[{"start_s": 6.0, "end_s": 38.0}],
            ),
            track("missing-rb-bpm", rb_bpm_x100=None, rb_pvdi=False),
            track("missing-rb-key", rb_key_scale_name=None, rb_pvdi=False),
            track(
                "no-own-bpm",
                own_bpm=None,
                rb_pvdi=True,
                rb_vocal_regions=[{"start_s": 5.0, "end_s": 50.0}],
                own_vocal_regions=None,
            ),
            track(
                "no-own-key",
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pvdi=True,
                rb_vocal_regions=[{"start_s": 15.0, "end_s": 60.0}],
                own_vocal_regions=[{"start_s": 15.0, "end_s": 58.0}],
                duration_s=100.0,
            ),
            track("ungradable-key-all", rb_key_scale_name="All", rb_pvdi=False),
            track("absent-audio", present=False),
        ]
    )
