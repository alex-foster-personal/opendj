"""Where a track's comment rides the wire (CMDK-04).

The Cmd K v2 bar searches comments collection-wide from the index row, and the
inspector reads it off ``GET /tracks/{sid}``. A ``GET /tracks`` listing row has no
use for it: LIBEDIT-9179 hoisted ``comments`` onto ``TrackOut`` and so onto every
listing row, which is what turned the row byte budget and the unmapped-row checks red.

  - [if] a GET /tracks listing row carries comments again [then] fail, [else stop].
"""

from __future__ import annotations

import pytest

from apps.webui.server.models import TrackIndexItemOut, TrackListItemOut, TrackOut
from apps.webui.server.routes.tracks import INDEX_ROW_FIELDS

pytestmark = [pytest.mark.requirement("CMDK-13842")]


def test_comments_ride_the_index_and_single_track_but_not_the_listing_row() -> None:
    """[if] a listing row serializes comments, or the index or single-track read loses them [then] fail, [else stop]."""
    assert TrackListItemOut.model_fields["comments"].exclude is True
    assert "comments" not in TrackListItemOut.model_json_schema()["properties"]
    assert not TrackIndexItemOut.model_fields["comments"].exclude
    assert "comments" in INDEX_ROW_FIELDS
    assert not TrackOut.model_fields["comments"].exclude
