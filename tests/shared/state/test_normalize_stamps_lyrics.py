"""The lyric_verdict half of the stamp sweep (schema v10, D13.1).

Split out of ``test_normalize_stamps.py`` so that file stays under the
600-line gate; same fixtures, same subject.

Single-line intent:
  - if STAMP_COLUMNS' digest half stops being alphabetical then a new
    synced table can be appended anywhere and nobody can scan for it
  - if lyric_verdict is not in the digest set then the sweep's coverage of
    it is a lie
"""

from __future__ import annotations

from apps.shared.state import normalize_stamps
from apps.sync_hub import protocol as hub_protocol


def test_the_digest_half_of_the_swept_list_stays_alphabetical() -> None:
    """The convention the tuple's own layout asserts, made checkable.

    ``STAMP_COLUMNS`` is a hand-maintained mirror of ``DIGEST_TABLES`` x
    {updated_at, deleted_at}, ordered alphabetically by table so a reader can
    find an entry and an author can see where a new one goes. Nothing checked
    that, so the ordering was one careless append away from being decorative
    -- and a list nobody can scan is a list a table goes missing from. The
    ``local_changelog`` pair is deliberately excluded: it is appended after
    the digest set rather than sorted into it.
    """
    digest = set(hub_protocol.DIGEST_TABLES)
    tables = [table for table, _column in normalize_stamps.STAMP_COLUMNS
              if table in digest]
    assert tables == sorted(tables), (
        f"STAMP_COLUMNS' digest half is out of alphabetical order: {tables}"
    )
    assert "lyric_verdict" in digest, (
        "schema v10 put lyric_verdict in the sync set; if it is not in the "
        "digest set here, the rest of this module's coverage is a lie"
    )
