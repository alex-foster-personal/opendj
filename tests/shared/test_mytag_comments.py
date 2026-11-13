"""Pure render, strip and merge for the Add My Tag to Comments block (issue #10025).

[if] tags are merged into a comment [then] the user part stays byte-identical and one block trails it, [else stop].
"""

from __future__ import annotations

import pytest

from apps.shared.mytag_comments import merge_mytag_comments, render_mytag_block, strip_mytag_block

pytestmark = pytest.mark.requirement("META-14581")


def test_block_renders_in_the_rekordbox_wrapper() -> None:
    """[if] tags a and b are rendered [then] the block is exactly '/* a / b */', none renders empty, [else stop]."""
    assert render_mytag_block(["a", "b"]) == "/* a / b */", "if the wrapper is not /* a / b */ then broken"
    assert render_mytag_block([]) == "", "if no tags still renders a block then broken"


def test_merge_appends_after_the_user_text_byte_identical() -> None:
    """[if] a user comment gains tags [then] it reads '<user> /* a / b */' with the user text untouched, [else stop]."""
    user = "  Big room\tdrop  at 2:10 "
    merged = merge_mytag_comments(user, ["a", "b"], enabled=True)
    assert merged == f"{user} /* a / b */", "if the block is not appended after one space then broken"
    assert strip_mytag_block(merged) == user, "if stripping does not give back the exact user text then broken"


def test_merge_is_idempotent_and_never_duplicates_the_block() -> None:
    """[if] an already merged comment is merged again with new tags [then] one fresh block trails it, [else stop]."""
    once = merge_mytag_comments("warm-up", ["a"], enabled=True)
    assert merge_mytag_comments(once, ["a"], enabled=True) == once, "if a re-merge changes the text then broken"
    assert merge_mytag_comments(once, ["a", "c"], enabled=True) == "warm-up /* a / c */", "if blocks stack then broken"


def test_off_or_no_tags_leaves_the_user_part_only() -> None:
    """[if] the setting is off or the tags are cleared [then] the block is removed, [else stop]."""
    merged = "warm-up /* a / b */"
    assert merge_mytag_comments(merged, ["a", "b"], enabled=False) == "warm-up", "if off keeps the block then broken"
    assert merge_mytag_comments(merged, [], enabled=True) == "warm-up", "if cleared tags keep a block then broken"
    assert merge_mytag_comments("/* a */", [], enabled=True) == "", "if a block-only comment is not emptied then broken"


def test_empty_and_missing_comments() -> None:
    """[if] the comment is None or empty [then] the block alone is written and None stays None when off, [else stop]."""
    assert merge_mytag_comments(None, ["a"], enabled=True) == "/* a */", "if None plus tags is not the bare block"
    assert merge_mytag_comments("", ["a"], enabled=True) == "/* a */", "if empty plus tags gains a space then broken"
    assert merge_mytag_comments(None, ["a"], enabled=False) is None, "if off turns None into text then broken"


def test_a_block_in_the_middle_of_user_text_is_left_alone() -> None:
    """[if] the user wrote '/* x */' mid-comment [then] strip keeps it, only a trailing block is ours, [else stop]."""
    user = "see /* x */ later"
    assert strip_mytag_block(user) == user, "if a mid-text block is stripped then broken"
    assert merge_mytag_comments(user, ["a"], enabled=True) == f"{user} /* a */", "if merge eats user text then broken"
