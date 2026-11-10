"""Track profiles, section labels and mix windows (AGENT-29) on invented tracks."""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from apps.autodj.contract.models import MikBoundary
from apps.autodj.profile import ProfileError, _camelot, _sections, profile_from_payloads
from apps.autodj.sections import own_label, phrase_label, pssi_label
from apps.autodj.windows import mix_windows, window_at
from tests.autodj.fixtures import CHORUS, DOWN, INTRO, OUTRO, UP, anlz_payload, profile, track_row


def test_pssi_high_mood_maps_to_dance_labels() -> None:
    assert pssi_label(INTRO, 1)[1] == "intro"
    assert pssi_label(UP, 1)[1] == "build"
    assert pssi_label(DOWN, 1)[1] == "breakdown"
    assert pssi_label(CHORUS, 1)[1] == "drop"
    assert pssi_label(OUTRO, 1)[1] == "outro"


def test_pssi_mid_mood_maps_to_song_labels_and_unknown_kinds_stay_unknown() -> None:
    labels = {pssi_label(kind, 2)[1] for kind in range(1, 11)}
    assert {"intro", "verse", "chorus", "bridge", "outro"} <= labels
    assert pssi_label(99, 1)[1] == "unknown"
    assert pssi_label(1, 99)[1] == "unknown"


def test_own_and_phrase_labels() -> None:
    assert own_label("Outro")[1] == "outro"
    assert phrase_label({"label": "intro"})[1] == "intro"
    assert phrase_label({"kind": OUTRO, "mood": 1})[1] == "outro"


@pytest.mark.parametrize(
    ("dj_label", "model_label", "expected"),
    [
        ("intro", "intro", "intro"),
        ("buildup", "verse", "build"),
        ("drop", "chorus", "drop"),
        ("breakdown", "bridge", "breakdown"),
        ("cooldown", "chorus", "outro"),
        ("outro", "outro", "outro"),
    ],
)
def test_own_dj_labels_reach_the_planner(dj_label: str, model_label: str, expected: str) -> None:
    """[if] an own phrase has a DJ label [then] it maps to its section, [else stop]."""
    phrase = {"start_s": 0.0, "end_s": 30.0, "kind": 0, "mood": 0, "source": "own"}
    phrase.update(label=dj_label, model_label=model_label, ambient=False)  # serve._own_phrase's shape
    assert phrase_label(phrase) == (dj_label, expected)


def test_an_unknown_dj_label_falls_back_to_the_model_label() -> None:
    """[if] a DJ label is unknown [then] the model's label decides, [else stop]."""
    assert phrase_label({"label": "lift", "model_label": "chorus", "source": "own"}) == ("lift", "chorus")
    assert phrase_label({"label": "lift", "model_label": "mystery", "source": "own"}) == ("lift", "unknown")
    # Control: a known DJ label wins over the model label.
    assert phrase_label({"label": "drop", "model_label": "verse", "source": "own"}) == ("drop", "drop")


def test_profile_reads_sections_vocals_and_relative_energy() -> None:
    track = profile("trk-a")
    assert track.bars == 128
    labels = [(section.label, section.start_bar) for section in track.sections]
    assert labels[:4] == [("intro", 1), ("build", 17), ("drop", 33), ("breakdown", 65)]
    drop = track.sections[2]
    assert drop.vocal_bars == 32
    assert track.sections[0].vocal_bars == 0
    assert track.bar_energy is not None
    assert max(track.bar_energy) == pytest.approx(1.0)  # normalized per track
    assert track.bar_energy[40] > track.bar_energy[70]  # drop louder than breakdown
    assert track.bar_bass is not None and track.bar_bass[40] > track.bar_bass[70]


def test_profile_bar_clock_round_trips() -> None:
    track = profile("trk-a", bpm=125.0, lead_in_s=0.4)
    assert track.bar_at(track.time_of_bar(17)) == pytest.approx(17.0)
    section = track.section_at(17.5)
    assert section is not None and section.label == "build"


def test_profile_without_vocal_analysis_says_so() -> None:
    track = profile("trk-a", vocals_analyzed=False)
    assert track.bar_vocals is None
    assert any("vocals not analyzed" in note for note in track.notes)


def test_profile_refuses_a_track_with_no_grid() -> None:
    anlz = anlz_payload("trk-a")
    anlz["beatgrid"]["beats"] = anlz["beatgrid"]["beats"][:3]
    with pytest.raises(ProfileError, match="downbeats"):
        profile_from_payloads(anlz, track_row("trk-a"))


def test_summary_never_carries_a_full_grid() -> None:
    summary = profile("trk-a").summary()
    assert "downbeats_s" not in summary and "bar_energy" not in summary
    assert "bar_mid" not in summary and "bar_high" not in summary
    assert summary["bars"] == 128


_GOLDEN_WITHOUT_MID_HIGH: dict[str, str] = {
    "trk-a": "6832985b9ac258511784a97c62c537722ee34fce14f86695b56972ed8c30c001",
    "trk-b": "d46d5e9f9ed93a2cfb87da31ab866040634ff17550cd4ab5624af770861d33e0",
    "trk-a-no-vocals": "362e1625e88e9cebebc009115875b93629ed343289c8deb444bad71b1c34e4ae",
    "mono": "451851e330bd511dd61ec4a1459c9339c202e28433025eec8d1c9a32914d253b",
    "absent": "36eae0f865f7b795bc6234258154cd4909dcdc5b236226c6ca6f302d07961dd9",
}
_TRK_A_SUMMARY_DIGEST = "c454f0fa90d8f3b3bae5e76a15be55e05b8fb4beaa1b81321a5f06efea1eed62"
_TRANSAI_REQ = "TRANSAI-10505"


def _profile_digest_without_new_bands(p) -> str:
    data = dataclasses.asdict(p)
    for key in (
        "bar_mid",
        "bar_high",
        "mik_bar_level",
        "mik_level",
        "mik_boundaries",
        "mik_grid_suspect",
        "mik_grid_note",
    ):
        data.pop(key, None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


@pytest.mark.requirement(_TRANSAI_REQ)
def test_profile_carries_mid_and_high_per_bar() -> None:
    """[if] the waveform has three bands [then] bar_mid and bar_high are per-bar 0..1 series, [else stop]."""
    track = profile("trk-a")
    assert track.bar_mid is not None and track.bar_high is not None
    assert len(track.bar_mid) == len(track.bar_high) == track.bars
    assert all(0.0 <= value <= 1.0 for value in track.bar_mid)
    assert all(0.0 <= value <= 1.0 for value in track.bar_high)
    assert max(track.bar_mid) == pytest.approx(1.0)
    assert max(track.bar_high) == pytest.approx(1.0)
    assert track.bar_mid[120] == pytest.approx(0.4)
    assert track.bar_high[120] == pytest.approx(0.4)
    assert track.bar_energy is not None and track.bar_bass is not None
    assert track.bar_energy[120] == pytest.approx(0.6)
    assert track.bar_bass[120] == pytest.approx(0.8)


def test_mid_and_high_use_their_own_peak() -> None:
    anlz = anlz_payload("trk-a")
    baseline = profile_from_payloads(anlz, track_row("trk-a"))
    scaled = dict(anlz)
    scaled["waveform"] = dict(anlz["waveform"])
    detail = dict(anlz["waveform"]["detail"])
    detail["high"] = [value * 0.25 for value in detail["high"]]
    scaled["waveform"]["detail"] = detail
    adjusted = profile_from_payloads(scaled, track_row("trk-a"))
    assert adjusted.bar_high == baseline.bar_high
    assert adjusted.bar_mid == baseline.bar_mid


@pytest.mark.requirement(_TRANSAI_REQ)
def test_mono_and_absent_waveforms_leave_mid_and_high_unknown() -> None:
    """[if] the waveform is mono or absent [then] bar_mid and bar_high are None, [else stop]."""
    anlz = anlz_payload("trk-a")
    mono = dict(anlz)
    mono["waveform"] = dict(anlz["waveform"])
    mono["waveform"]["detail"] = dict(anlz["waveform"]["detail"])
    mono["waveform"]["detail"]["mid"] = []
    mono["waveform"]["detail"]["high"] = []
    mono_track = profile_from_payloads(mono, track_row("trk-a"))
    assert mono_track.bar_bass is None
    assert mono_track.bar_mid is None
    assert mono_track.bar_high is None
    assert mono_track.bar_energy is not None
    absent = dict(anlz)
    absent["waveform"] = {}
    absent_track = profile_from_payloads(absent, track_row("trk-a"))
    assert absent_track.bar_energy is None
    assert absent_track.bar_bass is None
    assert absent_track.bar_mid is None
    assert absent_track.bar_high is None


@pytest.mark.requirement(_TRANSAI_REQ)
@pytest.mark.parametrize(
    ("case", "builder"),
    [
        ("trk-a", lambda: profile("trk-a")),
        ("trk-b", lambda: profile("trk-b")),
        ("trk-a-no-vocals", lambda: profile("trk-a", vocals_analyzed=False)),
        (
            "mono",
            lambda: profile_from_payloads(
                {
                    **anlz_payload("trk-a"),
                    "waveform": {
                        **anlz_payload("trk-a")["waveform"],
                        "detail": {
                            **anlz_payload("trk-a")["waveform"]["detail"],
                            "mid": [],
                            "high": [],
                        },
                    },
                },
                track_row("trk-a"),
            ),
        ),
        (
            "absent",
            lambda: profile_from_payloads({**anlz_payload("trk-a"), "waveform": {}}, track_row("trk-a")),
        ),
    ],
)
def test_existing_profile_fields_are_unchanged(case: str, builder) -> None:
    """[if] mid and high are added [then] every other field matches the pre-change golden, [else stop]."""
    built = builder()
    assert _profile_digest_without_new_bands(built) == _GOLDEN_WITHOUT_MID_HIGH[case]
    if case == "trk-a":
        summary_digest = hashlib.sha256(json.dumps(built.summary(), sort_keys=True).encode()).hexdigest()
        assert summary_digest == _TRK_A_SUMMARY_DIGEST
        anlz = anlz_payload("trk-a")
        first = profile_from_payloads(anlz, track_row("trk-a"))
        second = profile_from_payloads(anlz, track_row("trk-a"))
        assert first == second


def test_to_json_carries_the_per_bar_series() -> None:
    track = profile("trk-a")
    document = track.to_json()
    assert document["bar_mid"] == list(track.bar_mid or ())
    assert document["bar_high"] == list(track.bar_high or ())
    json.dumps(document)
    anlz = anlz_payload("trk-a")
    mono = dict(anlz)
    mono["waveform"] = dict(anlz["waveform"])
    mono["waveform"]["detail"] = dict(anlz["waveform"]["detail"])
    mono["waveform"]["detail"]["mid"] = []
    mono["waveform"]["detail"]["high"] = []
    mono_track = profile_from_payloads(mono, track_row("trk-a"))
    assert mono_track.to_json()["bar_mid"] is None
    assert dataclasses.replace(track, bar_mid=None, bar_high=None).time_of_bar(17) == track.time_of_bar(17)


@pytest.mark.requirement("AUTOPLAY-12017")
def test_an_outro_shorter_than_four_bars_is_marked_too_short() -> None:
    """[if] outro has under 4 bars left on the grid [then] too_short is set, [else stop]."""
    arrangement = (
        (INTRO, 16, 0.4, 0.2, False),
        (UP, 16, 0.6, 0.5, False),
        (CHORUS, 16, 1.0, 1.0, True),
        (OUTRO, 3, 0.4, 0.8, False),
    )
    track = profile("trk-short-outro", arrangement=arrangement)
    outro = next(window for window in mix_windows(track, "out") if window.label == "outro")
    assert outro.max_bars == 2
    assert outro.too_short is True


def test_out_windows_prefer_the_outro_and_end_by_the_last_downbeat() -> None:
    track = profile("trk-a")
    windows = mix_windows(track, "out")
    assert windows[0].label == "outro"
    for window in windows:
        assert window.start_bar + window.max_bars <= track.bars
    breakdown = next(window for window in windows if window.label == "breakdown")
    assert breakdown.vocal_free_bars == 32  # breakdown + build, then the vocal drop


def test_in_windows_prefer_the_intro_and_stop_before_the_first_vocal() -> None:
    windows = mix_windows(profile("trk-b"), "in")
    assert (windows[0].label, windows[0].start_bar) == ("intro", 1)
    assert windows[0].max_bars == 32  # bar 33 is the drop and the first vocal
    assert windows[0].peak_bar == 33


def test_mix_windows_without_phrases_fall_back() -> None:
    anlz = anlz_payload("trk-a")
    anlz["phrases"] = []
    track = profile_from_payloads(anlz, track_row("trk-a"))
    (out,) = mix_windows(track, "out")
    assert out.label == "unknown" and (out.start_bar - 1) % 8 == 0
    (in_,) = mix_windows(track, "in")
    assert in_.start_bar == 1


def test_window_at_validates_the_bar_and_role() -> None:
    track = profile("trk-a")
    assert window_at(track, "out", 65).label == "breakdown"
    with pytest.raises(ValueError, match="outside the grid"):
        window_at(track, "out", 500)
    with pytest.raises(ValueError, match="role"):
        mix_windows(track, "sideways")  # type: ignore[arg-type]


@pytest.mark.requirement("AGENT-29")
def test_no_out_window_is_advertised_from_the_last_downbeat() -> None:
    """[if] a window is asked for at the last downbeat [then] it is refused, since no bar is left to blend into, [else stop]."""
    track = profile("trk-a")
    assert all(window.start_bar < track.bars for window in mix_windows(track, "out"))
    with pytest.raises(ValueError, match="last downbeat"):
        window_at(track, "out", track.bars)
    assert window_at(track, "out", track.bars - 1).max_bars == 1  # control: one bar before the last is a window


@pytest.mark.requirement("AGENT-29")
def test_a_phrase_running_past_the_last_downbeat_keeps_the_final_bar() -> None:
    """[if] the last phrase runs past the final downbeat [then] its section keeps that bar, [else stop]."""
    downbeats = (0.0, 2.0, 4.0, 6.0)
    past = _sections([{"start_s": 0.0, "end_s": 9.0, "kind": 1}], downbeats, None, None, None)
    assert [(section.start_bar, section.bars) for section in past] == [(1, 4)]
    # Control: a phrase that ends exactly on the final downbeat stops before the final bar.
    ends_there = _sections([{"start_s": 0.0, "end_s": 6.0, "kind": 1}], downbeats, None, None, None)
    assert [(section.start_bar, section.bars) for section in ends_there] == [(1, 3)]


def test_an_unreadable_key_refuses_the_profile_rather_than_reading_as_unknown() -> None:
    """A key the analysis cannot read is malformed source data: it must not look like a track with no key."""
    assert _camelot("") is None, "a blank key is absent, not malformed"
    assert _camelot("8A") == "8A"
    with pytest.raises(ProfileError):
        _camelot("not a key")


def _mik_anlz(
    stable_id: str,
    segments: list[dict[str, object]],
    *,
    bpm: float = 120.0,
    bars: int = 8,
    level: int = 6,
) -> dict:
    anlz = anlz_payload(stable_id, bpm=bpm, arrangement=((INTRO, bars, 0.5, 0.5, False),))
    anlz["energy_source"] = "mik"
    anlz["energy_level"] = level
    anlz["energy_segments"] = segments
    return anlz


_TRANSAI_MIK_REQ = "TRANSAI-10662"


@pytest.mark.requirement(_TRANSAI_MIK_REQ)
def test_mik_fields_are_none_when_energy_source_is_none() -> None:
    """[if] energy_source is none [then] mik_bar_level and mik_level are None, [else stop]."""
    track = profile("trk-a")
    assert track.mik_bar_level is None
    assert track.mik_level is None
    assert track.mik_boundaries == ()
    assert track.mik_grid_suspect == 0
    assert track.mik_grid_note is None


@pytest.mark.requirement(_TRANSAI_MIK_REQ)
def test_mik_bar_levels_use_majority_mapping_and_boundaries_snap() -> None:
    """[if] MIK segments step mid-bar and on a downbeat [then] bars map by overlap and boundaries snap, [else stop]."""
    beat_s = 60.0 / 120.0
    bar_s = 4 * beat_s
    duration = 8 * bar_s
    anlz = _mik_anlz(
        "mik-majority",
        [
            {"start_s": 0.0, "end_s": 2.6, "level": 4, "source": "mik", "start_clamped": False},
            {"start_s": 2.6, "end_s": duration, "level": 8, "source": "mik", "start_clamped": False},
        ],
    )
    track = profile_from_payloads(anlz, track_row("mik-majority", bpm=120.0, bars=8))
    assert track.mik_level == 6
    assert track.mik_bar_level is not None
    assert track.mik_bar_level[0] == 4
    assert track.mik_bar_level[1] == 8
    assert track.mik_boundaries == (
        MikBoundary(bar=pytest.approx(2.3), snapped_bar=None, level_before=4, level_after=8),
    )
    anlz["energy_segments"] = [
        {"start_s": 0.0, "end_s": 4.0, "level": 5, "source": "mik", "start_clamped": False},
        {"start_s": 4.0, "end_s": duration, "level": 9, "source": "mik", "start_clamped": False},
    ]
    snapped = profile_from_payloads(anlz, track_row("mik-majority", bpm=120.0, bars=8))
    assert snapped.mik_boundaries[0].snapped_bar == 3


@pytest.mark.requirement(_TRANSAI_MIK_REQ)
def test_half_bar_mik_offset_sets_suspect_count_and_grid_note() -> None:
    """[if] MIK boundaries sit a constant half-bar off downbeats [then] suspect counts and grid_note names it, [else stop]."""
    beat_s = 60.0 / 120.0
    bar_s = 4 * beat_s
    starts = [bar_s * (index + 0.5) for index in range(3)]
    segments = [
        {"start_s": 0.0, "end_s": starts[0], "level": 4, "source": "mik", "start_clamped": False},
        {"start_s": starts[0], "end_s": starts[1], "level": 6, "source": "mik", "start_clamped": False},
        {"start_s": starts[1], "end_s": starts[2], "level": 7, "source": "mik", "start_clamped": False},
        {"start_s": starts[2], "end_s": 8 * bar_s, "level": 5, "source": "mik", "start_clamped": False},
    ]
    track = profile_from_payloads(_mik_anlz("mik-offset", segments), track_row("mik-offset", bpm=120.0, bars=8))
    assert all(boundary.snapped_bar is None for boundary in track.mik_boundaries)
    assert track.mik_grid_suspect == len(track.mik_boundaries)
    assert track.mik_grid_note == "MIK energy boundaries sit +0.50 bar from downbeats"


@pytest.mark.requirement(_TRANSAI_MIK_REQ)
def test_anlz_without_energy_keys_raises_profile_error() -> None:
    """[if] anlz lacks energy_source [then] profile_from_payloads raises ProfileError naming it, [else stop]."""
    anlz = anlz_payload("trk-a")
    del anlz["energy_source"]
    with pytest.raises(ProfileError, match="energy_source"):
        profile_from_payloads(anlz, track_row("trk-a"))


@pytest.mark.requirement(_TRANSAI_MIK_REQ)
def test_a_mik_series_without_a_scalar_keeps_the_level_null() -> None:
    """[if] MIK serves a series but no scalar [then] mik_level is None and mik_bar_level is kept, [else stop]."""
    beat_s = 60.0 / 120.0
    bar_s = 4 * beat_s
    duration = 8 * bar_s
    anlz = _mik_anlz(
        "mik-series-only",
        [
            {"start_s": 0.0, "end_s": 2.6, "level": 4, "source": "mik", "start_clamped": False},
            {"start_s": 2.6, "end_s": duration, "level": 8, "source": "mik", "start_clamped": False},
        ],
    )
    anlz["energy_level"] = None
    track = profile_from_payloads(anlz, track_row("mik-series-only", bpm=120.0, bars=8))
    assert track.mik_level is None
    assert track.mik_bar_level is not None
    assert track.mik_bar_level[0] == 4
    assert track.mik_bar_level[1] == 8
    assert len(track.mik_boundaries) == 1


@pytest.mark.requirement(_TRANSAI_MIK_REQ)
def test_a_mik_scalar_without_a_series_keeps_the_bar_levels_null() -> None:
    """[if] MIK serves a scalar but no series [then] mik_bar_level is None and mik_level is kept, [else stop]."""
    track = profile_from_payloads(_mik_anlz("mik-scalar-only", [], level=6), track_row("mik-scalar-only"))
    assert track.mik_level == 6
    assert track.mik_bar_level is None
    assert track.mik_boundaries == ()
    assert track.mik_grid_suspect == 0
    assert track.mik_grid_note is None


@pytest.mark.requirement(_TRANSAI_MIK_REQ)
def test_mik_source_with_neither_scalar_nor_series_raises() -> None:
    """[if] energy_source is mik with neither scalar nor series [then] profile_from_payloads raises ProfileError, [else stop]."""
    anlz = _mik_anlz("mik-empty", [])
    anlz["energy_level"] = None
    with pytest.raises(ProfileError, match="neither"):
        profile_from_payloads(anlz, track_row("mik-empty"))


@pytest.mark.requirement(_TRANSAI_MIK_REQ)
def test_a_non_int_mik_level_still_raises() -> None:
    """[if] MIK energy_level is a non-int scalar [then] profile_from_payloads raises ProfileError, [else stop]."""
    beat_s = 60.0 / 120.0
    bar_s = 4 * beat_s
    duration = 8 * bar_s
    segments = [
        {"start_s": 0.0, "end_s": duration, "level": 4, "source": "mik", "start_clamped": False},
    ]
    for bad in ("6", True):
        anlz = _mik_anlz("mik-bad-level", segments)
        anlz["energy_level"] = bad
        with pytest.raises(ProfileError, match="energy_level"):
            profile_from_payloads(anlz, track_row("mik-bad-level", bpm=120.0, bars=8))
