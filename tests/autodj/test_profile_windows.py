"""Track profiles, section labels and mix windows (AGENT-29) on invented tracks."""

from __future__ import annotations

import pytest

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
    assert summary["bars"] == 128


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
