"""Validate the open-dj JSON Schema + fixture documents (OPEN-03b, OPEN-03c).

Phase 16 ships docs + adapter pages under ``open-dj/adapters/`` and the
conformance corpus under ``tests/fixtures/conformance/``. These tests assert
two contracts:

  1. The Phase 15 JSON Schema at ``open-dj/schema/v0.2/open-dj.schema.json``
     is a well-formed JSON Schema 2020-12 document.
  2. Every Phase 16 conformance fixture parses as JSON (structural sanity;
     strict schema validation comes when we promote fixtures to a `kind`
     that the schema accepts).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("OPEN-03")

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "open-dj" / "schema" / "v0.2" / "open-dj.schema.json"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "conformance"


@pytest.mark.requirement("OPEN-03b")
def test_spec_schema_file_exists_or_skipped() -> None:
    """Phase 15 is expected to ship this file; we skip rather than fail when
    this Phase-16 test runs against a tree where Phase 15 has not yet
    committed its schema artefact."""
    if not SCHEMA_PATH.exists():
        pytest.skip(f"Phase 15 schema not at {SCHEMA_PATH}; skipping until shipped")


# REQ: OPEN-03b
@pytest.mark.requirement("OPEN-03b")
def test_spec_schema_is_draft_2020_12() -> None:
    if not SCHEMA_PATH.exists():
        pytest.skip("Phase 15 schema not yet committed")
    data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert data.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert data.get("$id", "").endswith("open-dj.schema.json")


# REQ: OPEN-03b
@pytest.mark.requirement("OPEN-03b")
def test_spec_schema_passes_meta_schema() -> None:
    """The spec schema must itself be a valid JSON Schema 2020-12 document."""
    if not SCHEMA_PATH.exists():
        pytest.skip("Phase 15 schema not yet committed")
    try:
        from jsonschema.validators import Draft202012Validator
    except ImportError:
        pytest.skip("jsonschema not installed; Phase 15 owns that dep")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    # check_schema raises on the first structural problem.
    Draft202012Validator.check_schema(schema)


@pytest.mark.requirement("OPEN-03c")
@pytest.mark.parametrize("fixture_dir", sorted(
    [p.name for p in FIXTURE_ROOT.iterdir() if p.is_dir() and not p.name.startswith("_")]
    if FIXTURE_ROOT.exists() else []
))
def test_fixture_documents_parse(fixture_dir: str) -> None:
    """Every conformance fixture's expected.opendj.json is valid JSON."""
    path = FIXTURE_ROOT / fixture_dir / "expected.opendj.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("version") == "0.1"
    assert isinstance(data.get("tracks"), list)
    assert isinstance(data.get("playlists"), list)


@pytest.mark.requirement("OPEN-03b")
def test_adapter_docs_exist() -> None:
    """Phase 16 ships per-adapter docs for Serato + Traktor."""
    adapters_dir = REPO_ROOT / "open-dj" / "adapters"
    assert adapters_dir.exists()
    assert (adapters_dir / "serato.md").exists()
    assert (adapters_dir / "traktor.md").exists()


# REQ: OPEN-03b
@pytest.mark.requirement("OPEN-03b")
def test_licences_page_exists() -> None:
    """open-dj/LICENCES.md lists the licence posture Phase 16 ships under."""
    path = REPO_ROOT / "open-dj" / "LICENCES.md"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    # Sanity: mentions both the spec licence + the code licence.
    assert "CC-BY-4.0" in text
    assert "Apache-2.0" in text
    # Sanity: names the Serato third-party references.
    assert "triseratops" in text
