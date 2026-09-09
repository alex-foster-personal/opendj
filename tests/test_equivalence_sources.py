"""Matcher tests (PR #383 review): a colliding fuzzy key must never be
resolved by first-wins.

``sources.match`` runs three tiers, best first: ``exact_path``, ``basename``,
``artist_title``. The last two are fuzzy by construction (multiple rekordbox
rows can share a basename or a normalized artist/title), so a key that maps
to MORE THAN ONE rekordbox row carries no signal about which one is right.
Silently keeping whichever row SQLite returned first manufactures a pairing
the data never proved, and that fabricated pairing then feeds the agreement
rate and the verdict gate.
"""
from __future__ import annotations

from apps.equivalence.sources import MikRow, RbRow, match


def _rb(content_id: str, *, path: str, file_name: str, artist: str, title: str) -> RbRow:
    return RbRow(
        content_id=content_id,
        path=path,
        file_name=file_name,
        title=title,
        artist=artist,
        bpm_raw=12800,
        key_raw="8B",
        length_raw=200,
        rating=3,
    )


def _mik(pk: int, *, path: str | None, artist: str, title: str) -> MikRow:
    return MikRow(
        pk=pk,
        path=path,
        title=title,
        artist=artist,
        key_camelot="8B",
        energy=5.0,
        tempo=128.0,
        volume=-10.0,
        rating=3,
        key_confidence=1.0,
        analysed_span_s=200.0,
    )


def test_a_basename_shared_by_two_rekordbox_rows_matches_neither():
    rb_a = _rb(
        "rb-a", path="/A/song.flac", file_name="song.flac",
        artist="Artist A", title="Title A",
    )
    rb_b = _rb(
        "rb-b", path="/B/song.flac", file_name="song.flac",
        artist="Artist B", title="Title B",
    )
    # No exact-path hit, and no artist/title hit either: only the basename
    # tier could resolve this MIK row, and it is exactly the ambiguous one.
    mik = _mik(1, path="/unresolved/song.flac", artist="Nobody", title="Nothing")

    pairings, report = match([rb_a, rb_b], [mik])

    assert pairings == []
    assert report.by_tier["basename"] == 0
    assert report.unmatched_mik == 1


def test_an_artist_title_key_shared_by_two_rekordbox_rows_matches_neither():
    rb_a = _rb(
        "rb-a", path="/A/one.flac", file_name="one.flac",
        artist="Same Artist", title="Same Title",
    )
    rb_b = _rb(
        "rb-b", path="/B/two.flac", file_name="two.flac",
        artist="Same Artist", title="Same Title",
    )
    mik = _mik(2, path=None, artist="Same Artist", title="Same Title")

    pairings, report = match([rb_a, rb_b], [mik])

    assert pairings == []
    assert report.by_tier["artist_title"] == 0
    assert report.unmatched_mik == 1


def test_a_basename_held_by_exactly_one_rekordbox_row_still_matches():
    """Positive control: the collision guard must not swallow a clean match."""
    rb = _rb(
        "rb-only", path="/A/unique.flac", file_name="unique.flac",
        artist="Solo Artist", title="Solo Title",
    )
    mik = _mik(3, path="/elsewhere/unique.flac", artist="Nobody", title="Nothing")

    pairings, report = match([rb], [mik])

    assert len(pairings) == 1
    assert pairings[0].tier == "basename"
    assert report.by_tier["basename"] == 1


def test_two_mik_rows_claiming_one_rekordbox_row_keep_the_stronger_key_confidence():
    """Many-to-one MIK duplication (PR #383 review) must rank, not first-wins.

    Both MIK rows resolve to the same rekordbox row on the basename tier.
    Iteration order alone must not decide the winner: the weaker row is
    listed FIRST here, so a first-wins matcher would (wrongly) keep it.
    """
    rb = _rb(
        "rb-only", path="/A/unique.flac", file_name="unique.flac",
        artist="Solo Artist", title="Solo Title",
    )
    weak = _mik(10, path="/weak/unique.flac", artist="Nobody", title="Nothing")
    weak = MikRow(**{**weak.__dict__, "key_confidence": 0.2, "analysed_span_s": 30.0})
    strong = _mik(11, path="/strong/unique.flac", artist="Nobody", title="Nothing")
    strong = MikRow(**{**strong.__dict__, "key_confidence": 0.9, "analysed_span_s": 200.0})

    pairings, report = match([rb], [weak, strong])

    assert len(pairings) == 1
    assert pairings[0].right.pk == strong.pk
    assert report.matched_mik == 1
    assert report.unmatched_mik == 1


def test_collision_loser_does_not_leak_into_a_weaker_tier():
    """P1 regression (PR #383 review): the claimant-ranking fix marked only
    the WINNER as matched, so a losing duplicate from a strong tier was still
    eligible at a weaker tier and could be paired to a wholly unrelated
    rekordbox row there, contaminating the equivalence evidence.
    """
    rb_a = _rb(
        "rb-a", path="/A/exact.flac", file_name="unrelated.flac",
        artist="Artist A", title="Title A",
    )
    rb_c = _rb(
        "rb-c", path="/C/other.flac", file_name="exact.flac",
        artist="Wrong Artist", title="Wrong Title",
    )
    winner = _mik(1, path="/A/exact.flac", artist="Nobody", title="Nothing")
    winner = MikRow(**{**winner.__dict__, "key_confidence": 0.9, "analysed_span_s": 200.0})
    loser = _mik(2, path="/A/exact.flac", artist="Nobody2", title="Nothing2")
    loser = MikRow(**{**loser.__dict__, "key_confidence": 0.1, "analysed_span_s": 10.0})

    pairings, report = match([rb_a, rb_c], [winner, loser])

    assert len(pairings) == 1
    assert pairings[0].tier == "exact_path"
    assert pairings[0].right.pk == winner.pk
    assert report.by_tier["basename"] == 0
    assert report.matched_mik == 1
    assert report.unmatched_mik == 1


def test_a_row_already_claimed_at_a_stronger_tier_does_not_leak_to_a_weaker_one():
    """P1 regression (PR #383 review, fresh evidence): a MIK row whose
    STRONGEST hit at a tier is already claimed by a different mik row (won
    at an earlier, stronger tier) must be resolved as lost there, not fall
    through and pair with an unrelated rekordbox row at a weaker tier.
    """
    rb_a = _rb(
        "rb-a", path="/A/exact.flac", file_name="shared.flac",
        artist="Artist A", title="Title A",
    )
    rb_b = _rb(
        "rb-b", path="/B/other.flac", file_name="other.flac",
        artist="Wrong Artist", title="Wrong Title",
    )
    winner = _mik(1, path="/A/exact.flac", artist="Nobody", title="Nothing")
    # Same basename as rb_a ("shared.flac"), but a DIFFERENT path, so it
    # only clears the (weaker) basename tier against rb_a, whose exact_path
    # claim already belongs to `winner`. Its artist/title also happens to
    # collide with rb_b, so a leak would wrongly pair it there.
    leaker = _mik(2, path="/elsewhere/shared.flac", artist="Wrong Artist", title="Wrong Title")

    pairings, report = match([rb_a, rb_b], [winner, leaker])

    assert len(pairings) == 1
    assert pairings[0].tier == "exact_path"
    assert pairings[0].right.pk == winner.pk
    assert report.by_tier["basename"] == 0
    assert report.by_tier["artist_title"] == 0
    assert report.matched_mik == 1
    assert report.unmatched_mik == 1


def test_duplicate_claimant_ranking_is_deterministic_on_a_confidence_tie():
    """A tie on key_confidence must not depend on dict/iteration order."""
    rb = _rb(
        "rb-only", path="/A/unique.flac", file_name="unique.flac",
        artist="Solo Artist", title="Solo Title",
    )
    a = _mik(20, path="/a/unique.flac", artist="Nobody", title="Nothing")
    b = _mik(21, path="/b/unique.flac", artist="Nobody", title="Nothing")

    pairings_1, _ = match([rb], [a, b])
    pairings_2, _ = match([rb], [b, a])

    assert pairings_1[0].right.pk == pairings_2[0].right.pk
