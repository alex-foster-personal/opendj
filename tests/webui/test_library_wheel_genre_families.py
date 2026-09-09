"""Acceptance tests for the LIBUX-06 canonical simple-genre grouping.

Requirements:
✔︎ Every family from the frontend's genre-color.ts FAMILY list ports 1:1 (name, color).
✔︎ An unmatched raw genre tag returns None rather than a guessed family.
✔︎ Matching is case-insensitive, same as the frontend implementation.

Acceptance tests:
[if] a raw genre tag matches a known family regex [then ⛔️] a different family or None is returned
[if] a raw genre tag matches no family [then ⛔️] a family is invented for it
[if] the raw genre tag is None or blank [then ⛔️] a family is returned
"""

from __future__ import annotations

from apps.library_wheel.genre_families import SIMPLE_GENRE_FAMILIES, simple_genre_family


def test_known_tags_resolve_to_expected_family() -> None:
    assert simple_genre_family("Drum & Bass") == ("drum-and-bass", "#5ee07a")
    assert simple_genre_family("UK Garage") == ("breakbeat-garage", "#f0c14a")
    assert simple_genre_family("Peak Time Techno") == ("techno", "#5ec8ff")
    assert simple_genre_family("Tech House") == ("tech-house", "#4ad0c8")
    assert simple_genre_family("Deep House") == ("deep-house", "#7aa8ff")
    assert simple_genre_family("Afro House") == ("house", "#ff9a4a")
    assert simple_genre_family("Psytrance") == ("trance", "#c084fc")
    assert simple_genre_family("Hardstyle") == ("hardstyle", "#ff5a5a")
    assert simple_genre_family("Riddim") == ("dubstep", "#a78bfa")
    assert simple_genre_family("UK Drill") == ("hip-hop", "#f472b6")
    assert simple_genre_family("Nu-Disco") == ("disco-funk-soul", "#fb923c")
    assert simple_genre_family("Lo-Fi") == ("ambient-downtempo", "#94a3b8")
    assert simple_genre_family("Big Room") == ("electro-edm", "#38bdf8")
    assert simple_genre_family("Dancehall") == ("reggae", "#86efac")
    assert simple_genre_family("Afrobeat") == ("jazz-latin-world", "#fbbf24")
    assert simple_genre_family("Indie Rock") == ("pop-rock-indie", "#e2e8f0")


def test_case_insensitive() -> None:
    assert simple_genre_family("techno") == simple_genre_family("TECHNO")


def test_unmatched_tag_returns_none() -> None:
    assert simple_genre_family("Spoken Word Poetry") is None


def test_blank_or_missing_tag_returns_none() -> None:
    assert simple_genre_family(None) is None
    assert simple_genre_family("") is None
    assert simple_genre_family("   ") is None


def test_family_table_is_16_entries_matching_frontend() -> None:
    # apps/webui/frontend/src/lib/rb/genre-color.ts FAMILY list, ported 1:1
    # (LIBUX-06 REQUIREMENTS.md: "do it once and record the definition").
    assert len(SIMPLE_GENRE_FAMILIES) == 16
    names = [f.name for f in SIMPLE_GENRE_FAMILIES]
    assert len(names) == len(set(names))
