"""fetch_hub_rows cursor progress guard (issue #13141).

[if] a digest has more rows than one page [then] every page is collected in order, [else stop].
[if] a cursor repeats or regresses [then] the walk is rejected, [else stop].
[if] the final page is read [then] the walk ends, [else stop].
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.sync_hub import digest_diff
from apps.sync_hub.transport import SyncTransportError

pytestmark = pytest.mark.requirement("CLOUDSYNC-13141")


class _PagedChannel:
    """Fake hub transport serving scripted ``GET /rows`` pages keyed by cursor."""

    def __init__(self, pages: dict[str | None, dict[str, Any]]) -> None:
        """Store the scripted pages and record each requested cursor."""
        self.pages = pages
        self.cursors: list[str | None] = []

    def get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """Return the scripted page for the request cursor."""
        cursor = params.get("cursor")
        self.cursors.append(cursor)
        return self.pages[cursor]


def _row(position: int) -> dict[str, Any]:
    """Build one wire row for playlist position ``position``."""
    return {"pk": ["pl", str(position)], "canonical_hex": f"hex{position}"}


def test_multi_page_digest_collects_every_row_in_order() -> None:
    """A digest larger than one page is walked to the final page exactly once."""
    channel = _PagedChannel(
        {
            None: {"rows": [_row(0), _row(1)], "next_cursor": '["pl","1"]'},
            '["pl","1"]': {"rows": [_row(2), _row(3)], "next_cursor": '["pl","3"]'},
            '["pl","3"]': {"rows": [_row(4)], "next_cursor": None},
        }
    )
    rows = digest_diff.fetch_hub_rows(channel, "m1", "playlist_memberships")  # type: ignore[arg-type]
    assert list(rows) == [("pl", str(i)) for i in range(5)]
    assert channel.cursors == [None, '["pl","1"]', '["pl","3"]']


def test_repeated_cursor_is_rejected() -> None:
    """A hub that hands back the same cursor twice raises instead of looping forever."""
    channel = _PagedChannel(
        {
            None: {"rows": [_row(0)], "next_cursor": '["pl","0"]'},
            '["pl","0"]': {"rows": [_row(1)], "next_cursor": '["pl","0"]'},
        }
    )
    with pytest.raises(SyncTransportError, match="did not progress"):
        digest_diff.fetch_hub_rows(channel, "m1", "playlist_memberships")  # type: ignore[arg-type]


def test_regressing_cursor_is_rejected() -> None:
    """A cursor that re-serves already collected rows raises a regression error."""
    channel = _PagedChannel(
        {
            None: {"rows": [_row(0), _row(1)], "next_cursor": '["pl","1"]'},
            '["pl","1"]': {"rows": [_row(1), _row(2)], "next_cursor": '["pl","2"]'},
        }
    )
    with pytest.raises(SyncTransportError, match="regressed"):
        digest_diff.fetch_hub_rows(channel, "m1", "playlist_memberships")  # type: ignore[arg-type]


def test_empty_page_with_cursor_is_rejected() -> None:
    """An empty page that still advertises a cursor is a non-progressing walk."""
    channel = _PagedChannel(
        {
            None: {"rows": [_row(0)], "next_cursor": '["pl","0"]'},
            '["pl","0"]': {"rows": [], "next_cursor": '["pl","9"]'},
        }
    )
    with pytest.raises(SyncTransportError, match="did not progress"):
        digest_diff.fetch_hub_rows(channel, "m1", "playlist_memberships")  # type: ignore[arg-type]
