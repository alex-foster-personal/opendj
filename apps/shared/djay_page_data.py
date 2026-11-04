"""Decode ``view_mediaItemPlaylistView_page.data`` int64-LE rowid arrays.

Supersedes: the ``_unpack_page_rowids`` body in ``apps/shared/djay_db.py`` and
the ``parse_page_data`` body in ``apps/sync/playlist_tsaf.py`` (issue #7875).
"""

from __future__ import annotations

import struct

__all__ = ["parse_page_data"]


def parse_page_data(data: bytes | None) -> list[int]:
    """Unpack a page BLOB into rowids (signed int64 little-endian).

    Returns ``[]`` for ``None``, empty input, or a byte length not divisible
    by 8.
    """
    if not data:
        return []
    n = len(data)
    if n % 8 != 0:
        return []
    count = n // 8
    if count == 0:
        return []
    return list(struct.unpack(f"<{count}q", data))
