"""Unit tests for shared lyrics JSON field parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.lyrics.json_parse import non_empty_string


def test_non_empty_string_returns_value() -> None:
    path = Path("(private path)")
    assert (
        non_empty_string({"stable_id": "abc"}, "stable_id", path, entry_label="lyrics-cache entry")
        == "abc"
    )


def test_non_empty_string_rejects_missing_or_blank() -> None:
    path = Path("(private path)")
    with pytest.raises(ValueError, match=r"lyrics-fetch entry .* has invalid stable_id"):
        non_empty_string({}, "stable_id", path, entry_label="lyrics-fetch entry")
    with pytest.raises(ValueError, match=r"lyrics-fetch entry .* has invalid stable_id"):
        non_empty_string({"stable_id": ""}, "stable_id", path, entry_label="lyrics-fetch entry")
