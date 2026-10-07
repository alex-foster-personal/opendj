"""Validate the DDJ-FLX4 theory-first expected map against the probe schema."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_DIR = REPO_ROOT / "tools" / "controller-probe"
SCHEMA_PATH = PROBE_DIR / "schema" / "expected_map.schema.json"
MAP_PATH = PROBE_DIR / "flx4_expected_map.json"
FIXTURE_PATH = PROBE_DIR / "fixtures" / "unknown_status_byte.json"
RESEARCH_NOTE = REPO_ROOT / "docs" / "controller" / "research" / "DDJ-FLX4-MIDI-Message-List.md"
EXPECTED_SHA = "e03ac376ebaa2d461c1a2c832c3c182e3579c893a1531944ebac1b5ce620388f"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.requirement("CTRL-01")
def test_expected_map_schema_compiles() -> None:
    """[if] the probe schema is not Draft 2020-12 [then] validation is undefined, [else stop]."""
    Draft202012Validator.check_schema(_load_json(SCHEMA_PATH))


@pytest.mark.requirement("CTRL-01")
def test_flx4_expected_map_validates() -> None:
    """[if] flx4_expected_map.json fails validation [then] the theory map is broken, [else stop]."""
    schema = _load_json(SCHEMA_PATH)
    doc = _load_json(MAP_PATH)
    Draft202012Validator(schema).validate(doc)


@pytest.mark.requirement("CTRL-01")
def test_control_count_matches_controls_length() -> None:
    """[if] control_count != len(controls) [then] the header lies about size, [else stop]."""
    doc = _load_json(MAP_PATH)
    assert doc["control_count"] == len(doc["controls"])


@pytest.mark.requirement("CTRL-01")
def test_control_ids_unique_and_per_deck_rows_present() -> None:
    """[if] play/cue/hot-cue pad1 miss either deck [then] expansion is incomplete, [else stop]."""
    doc = _load_json(MAP_PATH)
    ids = [c["id"] for c in doc["controls"]]
    assert len(ids) == len(set(ids))
    for deck in (1, 2):
        assert f"deck{deck}_play_pause" in ids
        assert f"deck{deck}_cue" in ids
        assert f"deck{deck}_hot_cue_pad1" in ids


@pytest.mark.requirement("CTRL-01")
def test_unknown_status_byte_fixture_raises_validation_error() -> None:
    """[if] status 0xC0 validates [then] unknown families are not refused, [else stop]."""
    schema = _load_json(SCHEMA_PATH)
    validator = Draft202012Validator(schema)
    bad = _load_json(FIXTURE_PATH)
    with pytest.raises(ValidationError):
        validator.validate(bad)


@pytest.mark.requirement("CTRL-01")
def test_source_sha256_matches_research_note() -> None:
    """[if] map sha256 drifts from the research note [then] provenance is broken, [else stop]."""
    doc = _load_json(MAP_PATH)
    assert doc["source"]["sha256"] == EXPECTED_SHA
    text = RESEARCH_NOTE.read_text(encoding="utf-8")
    assert EXPECTED_SHA in text
    assert re.search(r"control_count:\s*402", text)
