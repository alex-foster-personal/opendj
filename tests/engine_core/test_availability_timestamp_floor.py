"""Pure timestamp diagnostics for availability resolver invalidation.

[if] a track has the supported floor stamp [then] invalidation sorts earlier, [else stop].

Supersedes: the assumption that a 1970 checked_at predates every track stamp.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from apps.engine_core.library_availability import _STALE_CHECKED_AT
from apps.shared.state.sync_stamp import FLOOR_STAMP, canonical_from, parse_canonical

pytestmark = pytest.mark.requirement("LIBM-09")


def test_invalidation_precedes_the_supported_timestamp_floor() -> None:
    """Exercise SQLite's production strict timestamp comparison without track fixtures."""
    earliest = canonical_from(datetime.min.replace(tzinfo=UTC))
    assert earliest == FLOOR_STAMP
    assert parse_canonical(earliest) == datetime.min.replace(tzinfo=UTC)
    with sqlite3.connect(":memory:") as conn:
        assert conn.execute("SELECT ? > ?", (earliest, _STALE_CHECKED_AT)).fetchone() == (1,)
        assert conn.execute("SELECT ? > ?", (earliest, "1970-01-01T00:00:00+00:00")).fetchone() == (0,)
