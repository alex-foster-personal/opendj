"""Stem durable size vs source -- apps/stems/stem_size_policy.py.

WAV/FLAC-per-MP3 and fixed 192k Opus-per-stem both blow past the source.
These tests lock the input-aware bitrate split and the per-part size check.

Size-vs-source is a KPI/target, not a hard gate: assert_lossy_stems_not_
larger_than_source RETURNS a list of StemSizeViolation for any offending
part instead of raising -- the file is always kept. Only a genuine caller
contract bug (non-positive source_bytes/source_kbps, or no ladder rung
fits) still raises StemSizeError/ValueError.
"""

from __future__ import annotations

import pytest

from apps.stems.stem_size_policy import (
    MP3_CBR_LADDER,
    StemSizeError,
    StemSizeViolation,
    UnknownSourceFormatError,
    assert_lossy_stems_not_larger_than_source,
    effective_bitrate_kbps,
    lossy_stem_exceeds_source,
    lossy_stem_size_ratio,
    mp3_lame_settings_for_source_kbps,
    mp3_rung2_cbr_kbps,
    opus_ffmpeg_audio_args,
    opus_kbps_per_stem,
    stem_output_codec_for_source_ext,
)


# ----- bitrate from source ---------------------------------------------------
@pytest.mark.parametrize(
    "source_kbps,want",
    [
        (320, 80),  # warehouse mp3 -> four stems share the budget
        (256, 64),
        (192, 48),  # floor
        (128, 48),  # floor
        (64, 48),  # floor even when source/4 is lower
        (600, 128),  # cap: never one stem near a loud master alone
    ],
)
def test_opus_kbps_follows_source_with_floor_and_cap(source_kbps, want):
    assert opus_kbps_per_stem(source_kbps) == want


def test_opus_ffmpeg_args_carry_the_derived_bitrate():
    args = opus_ffmpeg_audio_args(320)
    assert args == ["-c:a", "libopus", "-b:a", "80k"]


def test_effective_bitrate_matches_size_over_duration():
    # 320 kbps for 150 s => 320000*150/8 = 6_000_000 bytes
    assert effective_bitrate_kbps(6_000_000, 150.0) == pytest.approx(320.0)


# ----- size check (KPI/target -- returns violations, never raises for a
#       genuine overage) ------------------------------------------------------
def test_each_lossy_stem_must_not_exceed_source_bytes():
    source = 6_000_000
    violations = assert_lossy_stems_not_larger_than_source(
        source_bytes=source,
        part_sizes={
            "vocals": 1_500_000,
            "drums": 1_400_000,
            "bass": 900_000,
            "other": 1_600_000,
        },
        codec="opus",
    )
    assert violations == []


def test_lossy_stem_larger_than_source_is_recorded_not_raised():
    """Size-vs-source is a target: an offending part comes back as a
    StemSizeViolation, the caller keeps the file."""
    violations = assert_lossy_stems_not_larger_than_source(
        source_bytes=6_000_000,
        part_sizes={"vocals": 6_000_001, "drums": 1, "bass": 1, "other": 1},
        codec="opus",
    )
    assert violations == [
        StemSizeViolation(
            name="vocals", part_bytes=6_000_001, source_bytes=6_000_000,
            ratio=pytest.approx(6_000_001 / 6_000_000),
        )
    ]


def test_flac_control_arm_skips_the_size_check():
    """FLAC is for SI-SDR / L rung; size vs MP3 is not the product constraint."""
    violations = assert_lossy_stems_not_larger_than_source(
        source_bytes=1_000,
        part_sizes={
            "vocals": 50_000_000,
            "drums": 50_000_000,
            "bass": 50_000_000,
            "other": 50_000_000,
        },
        codec="flac",
    )
    assert violations == []


def test_assert_lossy_stems_rejects_non_positive_source_bytes():
    """A non-positive source_bytes is a caller contract bug, not a size
    target miss -- this still raises."""
    with pytest.raises(StemSizeError, match="source_bytes"):
        assert_lossy_stems_not_larger_than_source(
            source_bytes=0, part_sizes={"vocals": 1}, codec="mp3"
        )


def test_four_stems_at_policy_bitrate_are_zero_sum_with_source():
    """Sanity: 4 * (source_kbps/4) == source_kbps inside the floor/cap band."""
    assert opus_kbps_per_stem(320) * 4 == 320
    assert opus_kbps_per_stem(256) * 4 == 256


# ----- RoFormer spike: source format -> output codec -------------------------
@pytest.mark.parametrize(
    "source_ext,want",
    [
        (".flac", "flac"),
        (".wav", "flac"),
        (".aif", "flac"),
        (".aiff", "flac"),
        (".FLAC", "flac"),  # case-insensitive
        (".mp3", "mp3"),
        (".m4a", "mp3"),
        (".MP3", "mp3"),  # case-insensitive
    ],
)
def test_output_codec_follows_source_lossless_vs_lossy(source_ext, want):
    assert stem_output_codec_for_source_ext(source_ext) == want


def test_unknown_source_extension_fails_fast():
    with pytest.raises(UnknownSourceFormatError, match=r"\.ogg"):
        stem_output_codec_for_source_ext(".ogg")


# ----- RoFormer spike: mp3 settings from probed source bitrate --------------
@pytest.mark.parametrize(
    "source_kbps,want",
    [
        (320, "lame_320kbps_cbr"),  # source can absorb 320 CBR exactly
        (500, "lame_320kbps_cbr"),  # source well above 320
        (319.99, "lame_v0_vbr"),  # just under the CBR floor
        (256, "lame_v0_vbr"),  # warehouse-typical mp3
        (128, "lame_v0_vbr"),  # low-bitrate mp3
    ],
)
def test_mp3_settings_follow_source_bitrate(source_kbps, want):
    assert mp3_lame_settings_for_source_kbps(source_kbps) == want


def test_mp3_settings_reject_non_positive_source_kbps():
    with pytest.raises(ValueError, match="source_kbps"):
        mp3_lame_settings_for_source_kbps(0)


def test_mp3_stems_are_checked_the_same_as_opus():
    """mp3 durable stems share opus's "no bigger than source" TARGET (same
    violations-returned, not-raised, contract)."""
    violations = assert_lossy_stems_not_larger_than_source(
        source_bytes=1_000_000,
        part_sizes={"vocals": 1_000_001, "instrumental": 1},
        codec="mp3",
    )
    assert [v.name for v in violations] == ["vocals"]


def test_mp3_stems_within_source_pass_the_check():
    violations = assert_lossy_stems_not_larger_than_source(
        source_bytes=1_000_000,
        part_sizes={"vocals": 400_000, "instrumental": 900_000},
        codec="mp3",
    )
    assert violations == []


# ----- per-part size ratio ----------------------------------------------------
def test_lossy_stem_size_ratio_exact_match_is_one():
    assert lossy_stem_size_ratio(source_bytes=1_000_000, part_bytes=1_000_000) == 1.0


def test_lossy_stem_size_ratio_double_source_is_two():
    assert lossy_stem_size_ratio(source_bytes=1_000_000, part_bytes=2_000_000) == 2.0


def test_lossy_stem_size_ratio_rejects_non_positive_source_bytes():
    with pytest.raises(StemSizeError, match="source_bytes"):
        lossy_stem_size_ratio(source_bytes=0, part_bytes=1)


# ----- two-rung mp3 encode ladder --------------------------------------------
@pytest.mark.parametrize(
    "source_bytes,part_bytes,codec,want",
    [
        (1_000_000, 1_000_001, "mp3", True),  # over by one byte
        (1_000_000, 1_000_000, "mp3", False),  # exactly equal is not "exceeds"
        (1_000_000, 999_999, "mp3", False),
        (1_000_000, 2_000_000, "opus", True),
        (1_000, 50_000_000, "flac", False),  # control codec, never gated
        (1_000, 50_000_000, "wav", False),
    ],
)
def test_lossy_stem_exceeds_source(source_bytes, part_bytes, codec, want):
    assert (
        lossy_stem_exceeds_source(
            source_bytes=source_bytes, part_bytes=part_bytes, codec=codec
        )
        is want
    )


def test_lossy_stem_exceeds_source_rejects_non_positive_source_bytes():
    with pytest.raises(StemSizeError, match="source_bytes"):
        lossy_stem_exceeds_source(source_bytes=0, part_bytes=1, codec="mp3")


@pytest.mark.parametrize(
    "source_kbps,want",
    [
        (257, 256),  # the observed 23-track failure band (~257-262 kbps)
        (262, 256),
        (259.99, 256),
        (320, 320),  # native-320 sibling case: same rung as rung 1 (known limit)
        (500, 320),
        (300, 256),
        (250, 224),
        (200, 192),  # exact floor rung
        (192, 192),
    ],
)
def test_mp3_rung2_cbr_kbps_picks_nearest_ladder_rung_at_or_under_source(
    source_kbps, want
):
    assert mp3_rung2_cbr_kbps(source_kbps) == want


def test_mp3_rung2_cbr_kbps_raises_below_the_ladder_floor():
    """A source under 192 kbps has no standard CBR rung to fall back to --
    a policy gap, not a guess."""
    with pytest.raises(StemSizeError, match="MP3_CBR_LADDER"):
        mp3_rung2_cbr_kbps(191.99)


def test_mp3_rung2_cbr_kbps_rejects_non_positive_source_kbps():
    with pytest.raises(ValueError, match="source_kbps"):
        mp3_rung2_cbr_kbps(0)


def test_mp3_cbr_ladder_is_descending_and_matches_rung1_cap():
    """MP3_CBR_LADDER's top rung must equal the rung-1 native CBR (both
    encode at the same 320 kbps ceiling) and stay strictly descending."""
    assert MP3_CBR_LADDER[0] == 320
    assert list(MP3_CBR_LADDER) == sorted(MP3_CBR_LADDER, reverse=True)


def test_ladder_decision_end_to_end_for_the_257_262kbps_failure_band():
    """Rung 1 (V0 VBR) has no ceiling, so it can land over source bytes;
    rung 2's chosen CBR must genuinely fit under the source's own kbps."""
    source_kbps = 260.0
    source_bytes = 6_000_000  # ~184.6s at 260 kbps
    rung1_bytes = 6_050_000  # observed shape: V0 landed slightly over source
    assert lossy_stem_exceeds_source(
        source_bytes=source_bytes, part_bytes=rung1_bytes, codec="mp3"
    )
    rung2_cbr = mp3_rung2_cbr_kbps(source_kbps)
    assert rung2_cbr == 256
    assert rung2_cbr < source_kbps  # the guarantee the module docstring claims


def test_ladder_decision_still_records_a_target_miss_if_rung2_is_not_enough():
    """Even at the terminal rung, an overage is recorded, never raised."""
    violations = assert_lossy_stems_not_larger_than_source(
        source_bytes=1_000_000,
        part_sizes={"vocals": 1_000_050, "instrumental": 900_000},
        codec="mp3",
    )
    assert len(violations) == 1
    assert violations[0].name == "vocals"
    assert violations[0].ratio > 1.0


