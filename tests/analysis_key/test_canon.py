"""Bidirectional canonicalizer tests for apps.analysis_key.canon.

The A minor case is the regression the lane exists to fix: the pre-existing
`_OPENKEY_MINOR` table in apps/analysis/backends/librosa.py mapped A minor
(pitch class 9) to Open Key "8m" by reusing the Camelot number directly. The
correct Open Key number is Camelot + 5 (mod 12, 1-indexed), which for 8A gives
1m. This file checks that rule for all twelve pitch classes in both major and
minor, not just A, because the underlying bug was a whole-table rotation, not
a single wrong entry.
"""
from __future__ import annotations

import pytest

from apps.analysis_key.canon import (
    Key,
    from_camelot,
    from_open_key,
    from_rekordbox_scale_name,
    to_camelot,
    to_open_key,
    to_rekordbox_scale_name,
)


def test_a_minor_is_camelot_8a_and_open_key_1m() -> None:
    a_minor = Key(pitch_class=9, is_minor=True)
    assert to_camelot(a_minor) == "8A"
    assert to_open_key(a_minor) == "1m"


def test_camelot_8a_round_trips_to_a_minor() -> None:
    key = from_camelot("8A")
    assert key == Key(pitch_class=9, is_minor=True)


def test_open_key_1m_round_trips_to_a_minor() -> None:
    key = from_open_key("1m")
    assert key == Key(pitch_class=9, is_minor=True)


def test_c_major_is_camelot_8b_and_open_key_1d() -> None:
    c_major = Key(pitch_class=0, is_minor=False)
    assert to_camelot(c_major) == "8B"
    assert to_open_key(c_major) == "1d"


@pytest.mark.parametrize("pitch_class", range(12))
def test_camelot_round_trips_for_every_pitch_class_major(pitch_class: int) -> None:
    key = Key(pitch_class=pitch_class, is_minor=False)
    assert from_camelot(to_camelot(key)) == key


@pytest.mark.parametrize("pitch_class", range(12))
def test_camelot_round_trips_for_every_pitch_class_minor(pitch_class: int) -> None:
    key = Key(pitch_class=pitch_class, is_minor=True)
    assert from_camelot(to_camelot(key)) == key


@pytest.mark.parametrize("pitch_class", range(12))
def test_open_key_round_trips_for_every_pitch_class_major(pitch_class: int) -> None:
    key = Key(pitch_class=pitch_class, is_minor=False)
    assert from_open_key(to_open_key(key)) == key


@pytest.mark.parametrize("pitch_class", range(12))
def test_open_key_round_trips_for_every_pitch_class_minor(pitch_class: int) -> None:
    key = Key(pitch_class=pitch_class, is_minor=True)
    assert from_open_key(to_open_key(key)) == key


@pytest.mark.parametrize("pitch_class", range(12))
def test_open_key_number_equals_camelot_number_plus_five_mod_twelve(pitch_class: int) -> None:
    """The rule the bug violated, checked directly rather than via a table."""
    for is_minor in (False, True):
        key = Key(pitch_class=pitch_class, is_minor=is_minor)
        camelot_num = int(to_camelot(key)[:-1])
        open_key_num = int(to_open_key(key)[:-1])
        assert open_key_num == ((camelot_num - 1 + 5) % 12) + 1


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Am", Key(9, True)),
        ("Bbm", Key(10, True)),
        ("Bm", Key(11, True)),
        ("C", Key(0, False)),
        ("Cm", Key(0, True)),
        ("D", Key(2, False)),
        ("Dbm", Key(1, True)),
        ("Dm", Key(2, True)),
        ("Em", Key(4, True)),
        ("F", Key(5, False)),
        ("F#m", Key(6, True)),
        ("Fm", Key(5, True)),
        ("Gm", Key(7, True)),
    ],
)
def test_rekordbox_spelled_names_from_real_fixture(name: str, expected: Key) -> None:
    """Every distinct spelled ScaleName value observed in
    /opt/mdt-fixtures/rekordbox/master.plain.db djmdKey (13 of its 21 rows;
    the other 8 rows already store Camelot strings directly, covered below).
    """
    assert from_rekordbox_scale_name(name) == expected


@pytest.mark.parametrize(
    "camelot_string",
    ["10A", "12A", "2B", "4A", "5A", "6A", "8A", "9A"],
)
def test_rekordbox_scale_name_also_accepts_camelot_strings(camelot_string: str) -> None:
    """8 of the 21 real rows in the fixture djmdKey table store a Camelot
    code directly in ScaleName instead of a spelled name (observed via
    `sqlite3 /opt/mdt-fixtures/rekordbox/master.plain.db "SELECT ScaleName FROM djmdKey"`).
    The parser must accept both shapes since real rekordbox libraries do.
    """
    assert from_rekordbox_scale_name(camelot_string) == from_camelot(camelot_string)


def test_rekordbox_scale_name_round_trips_through_canonical_spelling() -> None:
    key = Key(pitch_class=6, is_minor=True)
    name = to_rekordbox_scale_name(key)
    assert name == "F#m"
    assert from_rekordbox_scale_name(name) == key


@pytest.mark.parametrize("pitch_class", range(12))
def test_rekordbox_scale_name_round_trips_for_every_pitch_class(pitch_class: int) -> None:
    for is_minor in (False, True):
        key = Key(pitch_class=pitch_class, is_minor=is_minor)
        assert from_rekordbox_scale_name(to_rekordbox_scale_name(key)) == key


def test_key_rejects_out_of_range_pitch_class() -> None:
    with pytest.raises(ValueError):
        Key(pitch_class=12, is_minor=False)
    with pytest.raises(ValueError):
        Key(pitch_class=-1, is_minor=False)


def test_from_camelot_rejects_malformed_string() -> None:
    with pytest.raises(ValueError):
        from_camelot("13A")
    with pytest.raises(ValueError):
        from_camelot("8C")
    with pytest.raises(ValueError):
        from_camelot("not-a-key")


def test_from_open_key_rejects_malformed_string() -> None:
    with pytest.raises(ValueError):
        from_open_key("13m")
    with pytest.raises(ValueError):
        from_open_key("0d")


def test_from_rekordbox_scale_name_rejects_malformed_string() -> None:
    with pytest.raises(ValueError):
        from_rekordbox_scale_name("H")
    with pytest.raises(ValueError):
        from_rekordbox_scale_name("")
