"""LIBUX-10046: the Related Tracks rules, one point per enabled matching rule.

[if] a rule scores where its "no point when" case holds [then] Related ranks noise, [else stop].

Regression lines:
- if bpm_percent=4 accepts a 5 percent candidate then the range setting is ignored
- if an unrated seed matches on rating then every track scores a free point
- if "A feat. B" and "B & C" share no artist then credited artists are not split
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from apps.webui.server.related_tracks import (
    DEFAULT_RELATED_CRITERIA,
    RelatedCandidate,
    RelatedCriteria,
    credited_artists,
    matched_rules,
    rank_related,
)

pytestmark = pytest.mark.requirement("LIBUX-10046")

ALL_ON = RelatedCriteria(bpm_percent=6, key=True, genre=True, color=True, rating=True, mytag=True, artist=True)


def _cand(sid: str = "c", **kw: object) -> RelatedCandidate:
    base = RelatedCandidate(
        stable_id=sid,
        bpm=None,
        camelot=None,
        genre=None,
        rating=None,
        tags=frozenset(),
        artists=frozenset(),
        color_id=None,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("pct", "cand_bpm", "hit"),
    [(6, 105.0, True), (4, 105.0, False), (6, 50.0, True), (6, 200.0, True), (6, 120.0, False), (6, None, False)],
)
def test_bpm_range_with_half_and_double(pct: int, cand_bpm: float | None, hit: bool) -> None:
    """[if] bpm misses x0.5/x2 or ignores bpm_percent [then] the range rule is wrong, [else stop]."""
    seed = _cand("s", bpm=100.0)
    crit = replace(DEFAULT_RELATED_CRITERIA, bpm_percent=pct, key=False, genre=False)
    assert ("bpm" in matched_rules(seed, _cand(bpm=cand_bpm), crit)) is hit


@pytest.mark.parametrize(
    ("seed_kw", "cand_kw", "rule", "hit"),
    [
        ({"camelot": "8A"}, {"camelot": "9A"}, "key", True),
        ({"camelot": "8A"}, {"camelot": "3B"}, "key", False),
        ({"camelot": None}, {"camelot": "8A"}, "key", False),
        ({"genre": "house"}, {"genre": "house"}, "genre", True),
        ({"genre": None}, {"genre": None}, "genre", False),
        ({"color_id": 3}, {"color_id": 3}, "color", True),
        ({"color_id": 0}, {"color_id": 0}, "color", False),
        ({"rating": 3}, {"rating": 3}, "rating", True),
        ({"rating": 3}, {"rating": 5}, "rating", True),
        ({"rating": 3}, {"rating": 2}, "rating", False),
        ({"rating": 0}, {"rating": 5}, "rating", False),
        ({"tags": frozenset({"a", "b"})}, {"tags": frozenset({"b"})}, "mytag", True),
        ({"tags": frozenset()}, {"tags": frozenset()}, "mytag", False),
        ({"artists": credited_artists("A feat. B")}, {"artists": credited_artists("B & C")}, "artist", True),
        ({"artists": credited_artists("A")}, {"artists": credited_artists("C")}, "artist", False),
    ],
)
def test_each_rule(seed_kw: dict, cand_kw: dict, rule: str, hit: bool) -> None:
    """[if] a rule scores in its "no point when" case [then] Related ranks noise, [else stop]."""
    assert (rule in matched_rules(_cand("s", **seed_kw), _cand(**cand_kw), ALL_ON)) is hit


def test_disabled_rule_never_scores() -> None:
    """[if] a disabled rule scores [then] the cog toggles nothing, [else stop]."""
    seed, cand = _cand("s", genre="house"), _cand(genre="house")
    assert matched_rules(seed, cand, replace(DEFAULT_RELATED_CRITERIA, genre=False)) == ()


def test_rank_orders_score_then_bpm_distance_then_id_and_drops_seed_and_zero() -> None:
    """[if] ranking is not score, BPM distance, id [then] Related is unstable, [else stop]."""
    seed = _cand("s", bpm=100.0, genre="house")
    cands = [
        seed,
        _cand("b", bpm=103.0, genre="house"),
        _cand("a", bpm=103.0, genre="house"),
        _cand("c", bpm=101.0, genre="house"),
        _cand("d", bpm=101.0),
        _cand("z", bpm=150.0),
    ]
    ranked = rank_related(seed, cands, DEFAULT_RELATED_CRITERIA)
    assert [c.stable_id for c, _ in ranked] == ["c", "a", "b", "d"]
