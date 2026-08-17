"""Traktor field-mapper unit tests (OPEN-02d)."""

from __future__ import annotations

import pytest

from apps.adapters.traktor.mappers import (
    camelot_to_traktor_key,
    cue_color_from_type,
    opendj_cue_type_to_traktor,
    stars_to_traktor_rating,
    traktor_cue_type_to_opendj,
    traktor_key_to_camelot,
    traktor_rating_to_stars,
)

# ------------------------------------------------------------- key mapper

pytestmark = pytest.mark.requirement("OPEN-02")


@pytest.mark.requirement("OPEN-02d")
@pytest.mark.parametrize(
    "value, expected",
    [
        (0, "8B"),
        (7, "9B"),
        (12, "5A"),
        (21, "8A"),
        ("11", "1B"),
        (None, None),
        (99, None),
        ("not-a-number", None),
    ],
)
def test_traktor_key_to_camelot(value, expected) -> None:
    assert traktor_key_to_camelot(value) == expected


@pytest.mark.requirement("OPEN-02d")
def test_camelot_to_traktor_key_is_inverse() -> None:
    for i in range(24):
        camelot = traktor_key_to_camelot(i)
        assert camelot_to_traktor_key(camelot) == i


@pytest.mark.requirement("OPEN-02d")
@pytest.mark.parametrize("bad", [None, "", "ZZ", "13A"])
def test_camelot_to_traktor_key_handles_bad_input(bad) -> None:
    assert camelot_to_traktor_key(bad) is None


# ------------------------------------------------------------- rating map


@pytest.mark.requirement("OPEN-02d")
@pytest.mark.parametrize(
    "raw, expected",
    [
        (0, 0),
        (51, 1),
        (102, 2),
        (153, 3),
        (204, 4),
        (255, 5),
        ("0", 0),
        ("", None),
        (None, None),
        ("abc", None),
        # Fuzzier -- nearest slot
        (30, 1),
        (230, 5),
    ],
)
def test_traktor_rating_to_stars(raw, expected) -> None:
    assert traktor_rating_to_stars(raw) == expected


@pytest.mark.requirement("OPEN-02d")
def test_rating_is_inverse_within_canonical_values() -> None:
    for stars in range(6):
        raw = stars_to_traktor_rating(stars)
        assert traktor_rating_to_stars(raw) == stars


@pytest.mark.requirement("OPEN-02d")
def test_stars_to_traktor_rating_handles_none() -> None:
    assert stars_to_traktor_rating(None) is None


@pytest.mark.requirement("OPEN-02d")
def test_stars_to_traktor_rating_clips_out_of_range() -> None:
    assert stars_to_traktor_rating(-5) == 0
    assert stars_to_traktor_rating(99) == 255


# ---------------------------------------------------------- cue type map


@pytest.mark.requirement("OPEN-02d")
@pytest.mark.parametrize(
    "raw, expected",
    [
        (0, "hot"),
        (1, "fade_in"),
        (2, "fade_out"),
        (3, "load"),
        (4, "grid"),
        (5, "loop"),
        (None, "hot"),
        ("not-numeric", "hot"),
        (999, "hot"),  # fallback
    ],
)
def test_traktor_cue_type_to_opendj(raw, expected) -> None:
    assert traktor_cue_type_to_opendj(raw) == expected


@pytest.mark.requirement("OPEN-02d")
def test_opendj_cue_type_to_traktor_is_inverse() -> None:
    for raw_int in range(6):
        name = traktor_cue_type_to_opendj(raw_int)
        assert opendj_cue_type_to_traktor(name) == raw_int


@pytest.mark.requirement("OPEN-02d")
def test_cue_color_from_type_covers_known_types() -> None:
    # Each enum must have a palette entry.
    for name in ("hot", "loop", "grid", "load", "fade_in", "fade_out"):
        assert isinstance(cue_color_from_type(name), int)
    # Unknown cue type falls back to the hot colour.
    assert cue_color_from_type("mystery") == cue_color_from_type("hot")
