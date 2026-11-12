"""Pure ListenBrainz payload builder and submit eligibility (LIBM-12235).

[if] a play is heard long enough and not yet submitted [then] it is eligible once, [else stop].
"""

from __future__ import annotations

import pytest

from apps.scrobble.listenbrainz import (
    FOUR_MINUTES_MS,
    ListenPayloadError,
    Play,
    build_single_payload,
    is_submit_eligible,
)

pytestmark = pytest.mark.requirement("LIBM-12235")

LISTENED_AT: int = 1_700_000_000


def _play(play_id: str = "p1", duration_ms: int | None = 180_000, **overrides: str | None) -> Play:
    fields: dict[str, str | None] = {"artist": "Artist", "title": "Title", "release": "Album"}
    fields.update(overrides)
    return Play(play_id=play_id, listened_at=LISTENED_AT, duration_ms=duration_ms, **fields)


def test_half_of_short_track_is_eligible() -> None:
    """[if] a short track plays to half [then] a first submit is eligible, [else stop]."""
    play = _play(duration_ms=180_000)
    assert is_submit_eligible(play, 90_000, set())
    assert not is_submit_eligible(play, 89_999, set())


def test_four_minutes_of_long_track_is_eligible() -> None:
    """[if] a long track plays four minutes under half [then] a first submit is eligible, [else stop]."""
    play = _play(duration_ms=20 * 60 * 1000)
    assert is_submit_eligible(play, FOUR_MINUTES_MS, set())
    assert not is_submit_eligible(play, FOUR_MINUTES_MS - 1, set())


def test_submitted_play_identity_is_not_eligible_again() -> None:
    """[if] a play identity was already submitted [then] it is refused again, [else stop]."""
    play = _play(play_id="p-dup")
    assert is_submit_eligible(play, 180_000, set())
    assert not is_submit_eligible(play, 180_000, {"p-dup"})
    assert is_submit_eligible(_play(play_id="p-other"), 180_000, {"p-dup"})


@pytest.mark.parametrize("missing", ["artist", "title"])
def test_missing_artist_or_title_raises(missing: str) -> None:
    """[if] artist or title is absent [then] the builder raises, no payload, [else stop]."""
    with pytest.raises(ListenPayloadError):
        build_single_payload(_play(**{missing: None}))
    with pytest.raises(ListenPayloadError):
        build_single_payload(_play(**{missing: "  "}))


def test_single_payload_shape() -> None:
    """[if] a play has full metadata [then] the payload matches the documented single shape, [else stop]."""
    assert build_single_payload(_play()) == {
        "listen_type": "single",
        "payload": [
            {
                "listened_at": LISTENED_AT,
                "track_metadata": {
                    "artist_name": "Artist",
                    "track_name": "Title",
                    "release_name": "Album",
                    "additional_info": {"duration_ms": 180_000},
                },
            }
        ],
    }
