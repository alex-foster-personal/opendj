"""Unit tests for shared lyrics JSON field parsing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.lyrics.json_parse import (
    non_empty_string,
    require_object,
    require_schema,
    write_json_compact_atomically,
)


def test_non_empty_string_returns_value() -> None:
    path = Path("(private path)")
    assert non_empty_string({"stable_id": "abc"}, "stable_id", path, entry_label="lyrics-cache entry") == "abc"


def test_non_empty_string_rejects_missing_or_blank() -> None:
    path = Path("(private path)")
    with pytest.raises(ValueError, match=r"lyrics-fetch entry .* has invalid stable_id"):
        non_empty_string({}, "stable_id", path, entry_label="lyrics-fetch entry")
    with pytest.raises(ValueError, match=r"lyrics-fetch entry .* has invalid stable_id"):
        non_empty_string({"stable_id": ""}, "stable_id", path, entry_label="lyrics-fetch entry")


def test_require_object_rejects_non_object() -> None:
    path = Path("(private path)")
    with pytest.raises(TypeError, match=r"lyrics-cache entry .* must be a JSON object"):
        require_object([], path, entry_label="lyrics-cache entry")


def test_require_schema_rejects_mismatch() -> None:
    path = Path("(private path)")
    with pytest.raises(ValueError, match=r"lyrics-fetch entry .* has unsupported schema"):
        require_schema({"schema": 99}, path, 1, entry_label="lyrics-fetch entry")


def test_write_json_compact_atomically_writes_compact_newline(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "entry.json"
    write_json_compact_atomically(target, {"schema": 1, "stable_id": "abc"})
    assert target.read_text(encoding="utf-8") == '{"schema":1,"stable_id":"abc"}\n'
    assert json.loads(target.read_text(encoding="utf-8")) == {"schema": 1, "stable_id": "abc"}
