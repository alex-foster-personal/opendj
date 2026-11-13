"""Mirror a track's My Tags into its comment as a trailing ``/* a / b */`` block (rekordbox option, mimicked).

rekordbox Preferences > Advanced > My Tag > Add "My Tag" to the "Comments" appends the tags to the end of the
comment as ``/* tag1 / tag2 */`` so players show them, and drops the block when the option is turned off or the
tags are cleared. The user's own text before the block is never rewritten.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

_BLOCK_OPEN = "/* "
_BLOCK_CLOSE = " */"
_TAG_SEPARATOR = " / "
# The one block this module renders: at the very end, after at most one separating space.
_TRAILING_BLOCK = re.compile(r" ?/\* (?:(?!\*/).)* \*/\Z", re.DOTALL)


def render_mytag_block(tags: Sequence[str]) -> str:
    """The ``/* a / b */`` block for these tags, or an empty string when there are none."""
    if not tags:
        return ""
    return _BLOCK_OPEN + _TAG_SEPARATOR.join(tags) + _BLOCK_CLOSE


def strip_mytag_block(comments: str | None) -> str | None:
    """The user's own comment with any trailing My Tag block removed; ``None`` stays ``None``."""
    if comments is None:
        return None
    return _TRAILING_BLOCK.sub("", comments)


def merge_mytag_comments(comments: str | None, tags: Sequence[str], *, enabled: bool) -> str | None:
    """The comment a track should carry: user part plus the block when ``enabled``, the user part alone otherwise."""
    user_part = strip_mytag_block(comments)
    block = render_mytag_block(tags) if enabled else ""
    if not block:
        return user_part
    if not user_part:
        return block
    return f"{user_part} {block}"


__all__ = ["merge_mytag_comments", "render_mytag_block", "strip_mytag_block"]
