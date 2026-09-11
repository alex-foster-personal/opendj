"""Totality and correctness of the equivalence normalisers.

Regression lines (one per assertion, in the house "if X then broken" form):

- if a normaliser returns an unmapped value unchanged instead of raising then broken
- if fuzzing a normaliser's input domain leaks an input back out then broken
- if Camelot round-tripping through the canonical Key changes the key then broken
- if 8A and 1d (open key) do not canonicalise to the same key then broken
- if Am and 8A do not canonicalise to the same key then broken
- if Db and C# do not canonicalise to the same key (enharmonic) then broken
- if the relative of 8A is not 8B then broken
- if rekordbox centi-BPM 12400 does not canonicalise to 124.0 then broken
- if BPM 18000 declared as plain bpm does not raise then broken
- if energy 17 on a 1-10 scale does not raise then broken
- if POPM 128 (not one of the six steps) does not raise then broken
- if a MIK ZKEY of '0' is treated as a real key rather than MISSING then broken
- if beat-indexed time converts without a BPM then broken
- if an unknown loudness unit is silently compared then broken
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from apps.equivalence.normalisers import (
    KEY_SENTINELS,
    MISSING,
    NUMERIC_UNITS,
    REGISTERED_UNITS,
    Key,
    NormaliseError,
    is_comparable_unit,
    normalise,
    normalise_key,
    normalise_numeric,
    normalise_time,
    units_incompatible,
)

# --------------------------------------------------------------- key model


def test_camelot_round_trip_is_lossless():
    for number in range(1, 13):
        for letter in ("A", "B"):
            spelling = f"{number}{letter}"
            key = normalise_key(spelling, "camelot")
            assert isinstance(key, Key)
            assert key.camelot == spelling, (
                f"if Camelot round-tripping through the canonical Key changes "
                f"the key then broken: {spelling} -> {key.camelot}"
            )


def test_notations_agree_on_the_same_key():
    camelot = normalise_key("8A", "camelot")
    musical = normalise_key("Am", "musical")
    open_key = normalise_key("1m", "open_key")
    assert camelot == musical == open_key == Key(9, "min")


def test_enharmonics_collapse():
    assert normalise_key("Db", "musical") == normalise_key("C#", "musical")
    assert normalise_key("Ebm", "musical") == normalise_key("D#m", "musical")


def test_relative_major_of_8a_is_8b():
    minor = normalise_key("8A", "camelot")
    assert minor.relative.camelot == "8B"
    assert minor.relative.relative == minor


def test_mixed_column_accepts_both_notations_and_agrees():
    assert normalise_key("4A", "mixed_camelot_musical") == normalise_key(
        "Fm", "mixed_camelot_musical"
    )
    assert normalise_key("Fmaj", "mixed_camelot_musical") == normalise_key(
        "7B", "mixed_camelot_musical"
    )


def test_measured_sentinels_are_missing_not_keys():
    assert normalise_key("0", "camelot") is MISSING  # MIK ZKEY, 3 rows
    assert normalise_key("All", "mixed_camelot_musical") is MISSING  # ScaleName
    assert normalise_key(None, "camelot") is MISSING


def test_unmapped_key_raises_and_names_the_value():
    with pytest.raises(NormaliseError) as caught:
        normalise_key("H#dim7", "musical")
    assert "H#dim7" in str(caught.value)


def test_open_key_is_case_sensitive():
    # 'd' is major, 'm' is minor. Upper-casing the column would silently swap
    # the two, so an upper-case spelling must not quietly pass.
    assert normalise_key("1d", "open_key") == Key(0, "maj")
    with pytest.raises(NormaliseError):
        normalise_key("1D", "open_key")


# ------------------------------------------------------------- numerics


def test_rekordbox_centi_bpm_scales_to_bpm():
    assert normalise_numeric(12400, "bpm", "centi_bpm") == pytest.approx(124.0)


def test_bpm_18000_declared_as_bpm_raises():
    # The whole point of the declared-unit check: 18000 BPM is not a tempo.
    with pytest.raises(NormaliseError):
        normalise_numeric(18000, "bpm", "bpm")


def test_energy_index_masquerading_as_energy_raises():
    # DJ.Studio's energyLevelNr reaches 17 on what looks like an energy column.
    with pytest.raises(NormaliseError):
        normalise_numeric(17, "energy", "energy_1_10")


def test_popm_rejects_values_off_the_six_steps():
    assert normalise_numeric(204, "rating", "rekordbox_popm_0_255") == 4.0
    with pytest.raises(NormaliseError):
        normalise_numeric(128, "rating", "rekordbox_popm_0_255")


def test_documented_zero_sentinels_are_missing():
    assert normalise_numeric(0, "bpm", "centi_bpm") is MISSING
    assert normalise_numeric(0.0, "bpm", "bpm") is MISSING
    assert normalise_numeric(0, "duration", "seconds") is MISSING


def test_seconds_and_milliseconds_meet_in_the_middle():
    assert normalise_numeric(387, "duration", "seconds") == pytest.approx(387_000.0)
    assert normalise_numeric(387_000, "duration", "milliseconds") == pytest.approx(
        387_000.0
    )


def test_beat_indexed_time_needs_a_bpm():
    with pytest.raises(NormaliseError):
        normalise_time(16, "beats")
    assert normalise_time(16, "beats", bpm=120.0) == pytest.approx(8_000.0)


def test_absent_and_unknown_units_are_not_comparable():
    assert not is_comparable_unit("energy", "absent")
    assert not is_comparable_unit("loudness", "unknown_db_family")
    with pytest.raises(NormaliseError):
        normalise_numeric(-13.0, "loudness", "unknown_db_family")


def test_loudness_families_are_never_silently_interchanged():
    assert units_incompatible("loudness", "dbfs", "lufs_integrated") is not None
    assert units_incompatible("loudness", "dbfs", "dbfs") is None


# ---------------------------------------------------------------- fuzzing

# Values chosen to probe the whole input domain: sentinels, junk types, junk
# strings, extremes, and the near-misses that a sloppy normaliser waves through.
FUZZ_INPUTS: list[Any] = [
    None,
    "",
    " ",
    "0",
    "00",
    "-1",
    0,
    -1,
    1,
    5,
    9,
    10,
    11,
    17,
    51,
    128,
    255,
    256,
    -0.0,
    0.5,
    120.0,
    12400,
    18000,
    36_000_000,
    1e12,
    float("nan"),
    float("inf"),
    float("-inf"),
    True,
    False,
    [],
    {},
    (1, 2),
    "NaN",
    "abc",
    "8",
    "A",
    "8A",
    "13A",
    "0A",
    "8C",
    "1d",
    "1D",
    "1m",
    "Am",
    "Fmaj",
    "H",
    "All",
    "none",
    "8a",
    "  8A  ",
    "124",
    "124.5",
    "♭",
    "\U0001f3b5",
]


@pytest.mark.parametrize("kind", sorted(REGISTERED_UNITS))
def test_every_normaliser_is_total_over_the_fuzzed_domain(kind: str):
    """Every input either canonicalises, is MISSING, or raises NormaliseError.

    Critically it must never come back as the input itself: a silent
    pass-through of an unmapped value is the bug SKILL 4b step 5 forbids.
    """
    for unit in REGISTERED_UNITS[kind]:
        for value in FUZZ_INPUTS:
            try:
                result = normalise(value, kind, unit)
            except NormaliseError:
                continue  # the third legal outcome
            if result is MISSING:
                continue
            if kind == "key":
                assert isinstance(result, Key), (
                    f"if a normaliser returns an unmapped value unchanged "
                    f"instead of raising then broken: {kind}/{unit} "
                    f"{value!r} -> {result!r}"
                )
                continue
            assert isinstance(result, float) and math.isfinite(result), (
                f"if a normaliser returns a non-finite or non-float canonical "
                f"then broken: {kind}/{unit} {value!r} -> {result!r}"
            )
            assert not isinstance(result, str), "strings never survive as numerics"


def test_fuzzing_covers_every_registered_unit():
    """Guard the guard: a unit added without a fuzz pass is a silent gap."""
    registered = {(k, u) for k, units in REGISTERED_UNITS.items() for u in units}
    for kind in ("key", "bpm", "energy", "loudness", "duration", "rating", "time"):
        assert any(k == kind for k, _ in registered), f"{kind} has no units"
    assert set(KEY_SENTINELS) <= {u for k, u in registered if k == "key"}
    for kind, units in NUMERIC_UNITS.items():
        assert set(units) <= {u for k, u in registered if k == kind}


def test_bool_is_not_a_measurement():
    # True == 1 in Python, so a bool would sail through float() as a rating.
    with pytest.raises(NormaliseError):
        normalise_numeric(True, "rating", "stars_0_5")
