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
    rb_pwav: list[float] | None = None,
    rb_pwv2: list[float] | None = None,
    rb_pwv3: list[float] | None = None,
    rb_pwv4_luminance: list[float] | None = None,
    rb_pwv5: list[float] | None = None,
    rb_pwv6: dict[str, list[float]] | None = None,
    rb_pwv7: dict[str, list[float]] | None = None,
    own_preview: list[float] | None = None,
    own_detail: list[float] | None = None,
    own_triband: dict[str, list[float]] | None = None,
    include_waveform: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """One library-shaped row. Absent fields stay explicit None, never defaulted later."""
    row: dict[str, Any] = {
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
    if include_waveform:
        row.update(
            {
                "rb_pwav": rb_pwav,
                "rb_pwv2": rb_pwv2,
                "rb_pwv3": rb_pwv3,
                "rb_pwv4_luminance": rb_pwv4_luminance,
                "rb_pwv5": rb_pwv5,
                "rb_pwv6": rb_pwv6,
                "rb_pwv7": rb_pwv7,
                "own_preview": own_preview,
                "own_detail": own_detail,
                "own_triband": own_triband,
            }
        )
    row.update(extra)
    return row


def payload(
    tracks: list[dict[str, Any]], *, measured_at: str = MEASURED_AT, round: int = 0
) -> dict[str, Any]:
    """A scorable extract. Population totals are recorded so a scorer that
    divides by them can be caught; they are not the lane denominators.
    """
    body: dict[str, Any] = {
        "schema": 1,
        "measured_at": measured_at,
        "population": {
            "present": sum(1 for row in tracks if row["present"]),
            "tracks_rows": TRACKS_ROWS_TOTAL,
            "djmd_content_live": DJMD_CONTENT_LIVE_TOTAL,
        },
        "tracks": tracks,
    }
    if round:
        body["round"] = round
    return body


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


def _ramp(n: int = 8, *, start: float = 0.1, step: float = 0.1) -> list[float]:
    return [round(start + index * step, 4) for index in range(n)]


def _triband(
    n: int = 8,
    *,
    low_start: float = 0.1,
    mid_start: float = 0.2,
    high_start: float = 0.3,
    step: float = 0.1,
) -> dict[str, list[float]]:
    return {
        "low": [round(low_start + index * step, 4) for index in range(n)],
        "mid": [round(mid_start + index * step, 4) for index in range(n)],
        "high": [round(high_start + index * step, 4) for index in range(n)],
    }


def round1_fixture() -> dict[str, Any]:
    """Round-1 fixture covering waveform preview, detail, and tri-band cases."""
    preview_ramp = _ramp()
    detail_ramp = _ramp(start=0.2, step=0.08)
    triband_truth = _triband()
    reversed_preview = list(reversed(preview_ramp))
    partial_own = {
        "low": _ramp(start=0.9, step=-0.05),
        "mid": triband_truth["mid"],
        "high": _ramp(start=0.05, step=0.02),
    }
    return payload(
        [
            track("exact-128"),
            track(
                "exact-key-c",
                rb_key_scale_name="C",
                own_key_camelot="8B",
                own_key_openkey="1d",
            ),
            track(
                "wave-preview-exact",
                include_waveform=True,
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwav=preview_ramp,
                own_preview=preview_ramp,
                rb_pwv3=detail_ramp,
                own_detail=detail_ramp,
                rb_pwv6=triband_truth,
                own_triband=triband_truth,
            ),
            track(
                "wave-detail-exact",
                include_waveform=True,
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwav=preview_ramp,
                own_preview=preview_ramp,
                rb_pwv3=detail_ramp,
                own_detail=detail_ramp,
                rb_pwv6=triband_truth,
                own_triband=triband_truth,
            ),
            track(
                "wave-triband-exact",
                include_waveform=True,
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwav=preview_ramp,
                own_preview=preview_ramp,
                rb_pwv3=detail_ramp,
                own_detail=detail_ramp,
                rb_pwv6=triband_truth,
                own_triband=triband_truth,
            ),
            track(
                "wave-preview-reversed",
                include_waveform=True,
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwav=preview_ramp,
                own_preview=reversed_preview,
                rb_pwv3=detail_ramp,
                own_detail=detail_ramp,
                rb_pwv6=triband_truth,
                own_triband=triband_truth,
            ),
            track(
                "wave-no-own",
                include_waveform=True,
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwav=preview_ramp,
                own_preview=None,
                rb_pwv3=detail_ramp,
                own_detail=None,
                rb_pwv6=triband_truth,
                own_triband=None,
            ),
            track(
                "wave-unreadable-ext",
                include_waveform=True,
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_ext_readable=False,
                rb_pwav=preview_ramp,
                own_preview=preview_ramp,
                rb_pwv3=detail_ramp,
                own_detail=detail_ramp,
                rb_pwv6=triband_truth,
                own_triband=triband_truth,
            ),
            track(
                "wave-missing-preview",
                include_waveform=True,
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwav=None,
                rb_pwv2=None,
                own_preview=preview_ramp,
                rb_pwv3=detail_ramp,
                own_detail=detail_ramp,
                rb_pwv6=triband_truth,
                own_triband=triband_truth,
            ),
            track(
                "wave-missing-triband",
                include_waveform=True,
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwav=preview_ramp,
                own_preview=preview_ramp,
                rb_pwv3=detail_ramp,
                own_detail=detail_ramp,
                rb_pwv6=None,
                rb_pwv7=None,
                own_triband=triband_truth,
            ),
            track(
                "wave-missing-detail",
                include_waveform=True,
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwav=preview_ramp,
                own_preview=preview_ramp,
                rb_pwv3=None,
                rb_pwv4_luminance=None,
                rb_pwv5=None,
                own_detail=detail_ramp,
                rb_pwv6=triband_truth,
                own_triband=triband_truth,
            ),
            track(
                "wave-triband-partial",
                include_waveform=True,
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_pwav=preview_ramp,
                own_preview=preview_ramp,
                rb_pwv3=detail_ramp,
                own_detail=detail_ramp,
                rb_pwv6=triband_truth,
                own_triband=partial_own,
            ),
            track("absent-audio", present=False),
        ],
        round=1,
    )
