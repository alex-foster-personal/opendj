"""Listen history: parsing, ownership scoring, and the purchase-link contract.

Each test names the invariant it would catch breaking:
  if a malformed play is silently dropped instead of raising -> broken
  if an owned track renders a buy link set that differs from the queue -> broken
  if plays come back in anything but newest-first order -> broken
"""

from __future__ import annotations

import pytest

from apps.spotify.listen_history import build_recent_plays, parse_recent_items
from apps.spotify.matcher_adapter import LocalTrack
from apps.spotify.purchase_links import SUGGESTED_SOURCE_KEYS, suggested_sources
from apps.spotify.acquisition import SOURCE_KEYS as WRITER_KEYS


def _item(
    *,
    played_at: str,
    title: str,
    artist: str,
    isrc: str | None = None,
    duration_ms: int = 210_000,
    uri: str = "spotify:track:abc",
) -> dict:
    return {
        "played_at": played_at,
        "track": {
            "id": uri.rsplit(":", 1)[-1],
            "uri": uri,
            "name": title,
            "artists": [{"name": artist}],
            "album": {"name": "An Album"},
            "duration_ms": duration_ms,
            "external_ids": {"isrc": isrc} if isrc else {},
        },
    }


def _local(stable_id: str, title: str, artist: str, isrc: str | None, ms: int) -> LocalTrack:
    return LocalTrack(stable_id=stable_id, isrc=isrc, title=title, artists=(artist,), duration_ms=ms)


@pytest.mark.requirement("CAT-01")
def test_purchase_link_table_is_shared_with_the_persisted_queue() -> None:
    """The live pane and the persisted acquisition queue offer the same stores.

    [if] the live pane and the persisted queue build purchase links [then] both offer the same store keys and https URLs, [else stop]
    """
    assert SUGGESTED_SOURCE_KEYS == WRITER_KEYS
    links = suggested_sources("Aphex Twin", "Xtal")
    assert set(links) == set(SUGGESTED_SOURCE_KEYS)
    assert all(url.startswith("https://") for url in links.values())
    assert "Aphex+Twin+Xtal" in links["beatport"]


@pytest.mark.requirement("CAT-01")
def test_malformed_play_raises_rather_than_shrinking_the_list() -> None:
    """A dropped row would under-report what the operator actually played.

    [if] a play has no played_at, no track or a bad timestamp [then] parsing raises ValueError instead of dropping the row, [else stop]
    """
    with pytest.raises(ValueError, match="played_at"):
        parse_recent_items([{"track": {"uri": "spotify:track:x", "name": "n"}}])
    with pytest.raises(ValueError, match="no track"):
        parse_recent_items([{"played_at": "2026-09-22T10:00:00Z", "track": None}])
    with pytest.raises(ValueError):
        parse_recent_items([_item(played_at="not-a-timestamp", title="t", artist="a")])


@pytest.mark.requirement("CAT-01")
def test_isrc_match_reads_owned_and_carries_no_buy_prompt() -> None:
    """[if] a play shares its ISRC with a local track [then] it reads owned and names that track stable_id, [else stop]"""
    parsed = parse_recent_items(
        [_item(played_at="2026-09-22T10:00:00Z", title="Xtal", artist="Aphex Twin", isrc="GBAAA0000001")]
    )
    plays = build_recent_plays(parsed, [_local("local-1", "Xtal", "Aphex Twin", "GBAAA0000001", 210_000)])

    assert [play.ownership for play in plays] == ["owned"]
    assert plays[0].matched_stable_id == "local-1"


@pytest.mark.requirement("CAT-01")
def test_unknown_track_reads_unmatched_with_the_five_store_links() -> None:
    """[if] a play matches no local track [then] it reads unmatched with no stable_id and every suggested store link, [else stop]"""
    parsed = parse_recent_items(
        [_item(played_at="2026-09-22T10:00:00Z", title="Brand New Thing", artist="Nobody At All")]
    )
    plays = build_recent_plays(parsed, [_local("local-1", "Xtal", "Aphex Twin", None, 210_000)])

    assert plays[0].ownership == "unmatched"
    assert plays[0].matched_stable_id is None
    assert set(plays[0].suggested_sources) == set(SUGGESTED_SOURCE_KEYS)


@pytest.mark.requirement("CAT-01")
def test_plays_are_ordered_newest_first() -> None:
    """[if] plays arrive out of order [then] they come back newest first, [else stop]"""
    parsed = parse_recent_items(
        [
            _item(played_at="2026-09-22T09:00:00Z", title="Older", artist="A", uri="spotify:track:1"),
            _item(played_at="2026-09-22T11:00:00Z", title="Newest", artist="B", uri="spotify:track:2"),
            _item(played_at="2026-09-22T10:00:00Z", title="Middle", artist="C", uri="spotify:track:3"),
        ]
    )
    plays = build_recent_plays(parsed, [])

    assert [play.title for play in plays] == ["Newest", "Middle", "Older"]


@pytest.mark.requirement("CAT-01")
def test_empty_library_still_returns_every_play() -> None:
    """A library with nothing in it must not shrink the history to zero rows.

    [if] the local library is empty [then] every play is still returned, each unmatched, [else stop]
    """
    parsed = parse_recent_items(
        [
            _item(played_at="2026-09-22T10:00:00Z", title="One", artist="A", uri="spotify:track:1"),
            _item(played_at="2026-09-22T10:05:00Z", title="Two", artist="B", uri="spotify:track:2"),
        ]
    )
    plays = build_recent_plays(parsed, [])

    assert len(plays) == 2
    assert all(play.ownership == "unmatched" for play in plays)
