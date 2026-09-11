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


def hot_cue(
    in_ms: int,
    *,
    index: int | None = 0,
    name: str | None = None,
    kind: str = "hot",
    color_rgb: list[int] | None = None,
    loop_length_msec: int | None = None,
) -> dict[str, Any]:
    """One cue object in the PARITY-01 extract shape."""
    return {
        "in_ms": in_ms,
        "kind": kind,
        "index": index,
        "name": name,
        "color_rgb": color_rgb,
        "loop_length_msec": loop_length_msec,
    }


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
    rb_cue_db: bool | None = None,
    rb_cues_db: list[dict[str, Any]] | None = None,
    own_cues: list[dict[str, Any]] | None = None,
    rb_cues_pcob: list[dict[str, Any]] | None = None,
    rb_cues_pco2: list[dict[str, Any]] | None = None,
    rb_ext_readable: bool = True,
    rb_pvdi: bool = False,
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
        "rb_ext_readable": rb_ext_readable,
        "rb_pvdi": rb_pvdi,
    }
    if rb_cues_db is not None:
        row["rb_cues_db"] = rb_cues_db
        row["rb_cue_db"] = bool(rb_cues_db)
    elif rb_cue_db is not None:
        row["rb_cue_db"] = rb_cue_db
    else:
        row["rb_cue_db"] = False
    if own_cues is not None:
        row["own_cues"] = own_cues
    if rb_cues_pcob is not None:
        row["rb_cues_pcob"] = rb_cues_pcob
    if rb_cues_pco2 is not None:
        row["rb_cues_pco2"] = rb_cues_pco2
    return row


def payload(
    tracks: list[dict[str, Any]],
    *,
    measured_at: str = MEASURED_AT,
    round_n: int | None = None,
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
    if round_n is not None:
        body["round"] = round_n
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
        ],
        round_n=0,
    )


def round1_fixture() -> dict[str, Any]:
    """Cue lane cases for PARITY-01 round 1."""
    cue_a_1000 = hot_cue(1000, index=0)
    cue_a_5000 = hot_cue(5000, index=0)
    cue_2000 = hot_cue(2000, index=0)
    cue_2000_named = hot_cue(2000, index=0, name="drop")
    own_proposal = hot_cue(3000, index=None, name="intro")

    return payload(
        [
            track(
                "cue-db-exact",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cues_db=[cue_a_1000],
                own_cues=[cue_a_1000],
                rb_cues_pcob=[],
                rb_cues_pco2=[],
            ),
            track(
                "cue-db-disagree",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cues_db=[cue_a_1000],
                own_cues=[cue_a_5000],
                rb_cues_pcob=[],
                rb_cues_pco2=[],
            ),
            track(
                "cue-db-no-own",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cues_db=[cue_a_1000],
                own_cues=None,
                rb_cues_pcob=[],
                rb_cues_pco2=[],
            ),
            track(
                "cue-db-missing",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cue_db=False,
                own_cues=[own_proposal],
                rb_cues_pcob=[],
                rb_cues_pco2=[],
            ),
            track(
                "cue-anlz-pcob-exact",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cue_db=False,
                own_cues=[cue_2000],
                rb_cues_pcob=[cue_2000],
                rb_cues_pco2=[cue_2000_named],
            ),
            track(
                "cue-anlz-unreadable-ext",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cue_db=False,
                own_cues=[cue_2000],
                rb_cues_pcob=[cue_2000],
                rb_ext_readable=False,
            ),
            track(
                "cue-anlz-empty",
                rb_bpm_x100=None,
                own_bpm=None,
                rb_key_scale_name=None,
                own_key_camelot=None,
                own_key_openkey=None,
                rb_cue_db=False,
                own_cues=[],
                rb_cues_pcob=[],
                rb_cues_pco2=[],
            ),
            track("absent-audio", present=False),
        ],
        round_n=1,
    )
