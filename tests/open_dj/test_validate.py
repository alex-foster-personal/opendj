"""Tests for :mod:`apps.open_dj.validate` -- JSON Schema validation."""
from __future__ import annotations

import pytest

from apps.open_dj.schema_loader import load_schema
from apps.open_dj.validate import validate_document


# REQ: OPEN-03
# REQ: OPEN-03a
@pytest.mark.requirement("OPEN-01")
def test_schema_itself_is_valid_draft_2020_12() -> None:
    """Schema compiles under Draft202012Validator.check_schema."""
    from jsonschema.validators import Draft202012Validator
    Draft202012Validator.check_schema(load_schema())


@pytest.mark.requirement("OPEN-01")
def test_minimal_doc_validates(minimal_doc: dict) -> None:
    assert validate_document(minimal_doc) == []


@pytest.mark.requirement("OPEN-01")
def test_full_doc_validates(full_doc: dict) -> None:
    assert validate_document(full_doc) == []


@pytest.mark.requirement("OPEN-01")
def test_x_extension_doc_validates(ext_doc: dict) -> None:
    """x_* keys at any depth must pass validation (spec section 9)."""
    assert validate_document(ext_doc) == []


@pytest.mark.requirement("OPEN-01")
def test_wrapped_title_rejected(invalid_wrapped_doc: dict) -> None:
    """Spec section 6: identity fields MUST NOT be ProvenanceValue-wrapped."""
    errors = validate_document(invalid_wrapped_doc)
    assert errors, "validator should reject dict-valued title"
    assert any("title" in msg for msg in errors)


@pytest.mark.requirement("OPEN-01")
class TestRequiredFields:
    """Schema enforces required scalar fields on Track."""

    def test_missing_schema_version_rejected(self, minimal_doc: dict) -> None:
        doc = dict(minimal_doc)
        del doc["schema_version"]
        assert validate_document(doc)

    def test_wrong_schema_version_rejected(self, minimal_doc: dict) -> None:
        doc = dict(minimal_doc)
        doc["schema_version"] = "0.1"
        assert validate_document(doc)

    def test_missing_track_id_rejected(self, minimal_doc: dict) -> None:
        doc = dict(minimal_doc)
        doc["tracks"] = [dict(minimal_doc["tracks"][0])]
        del doc["tracks"][0]["track_id"]
        errors = validate_document(doc)
        assert errors

    def test_bad_track_id_pattern_rejected(self, minimal_doc: dict) -> None:
        doc = dict(minimal_doc)
        doc["tracks"] = [dict(minimal_doc["tracks"][0])]
        doc["tracks"][0]["track_id"] = "not-a-sha1"
        errors = validate_document(doc)
        assert errors
        assert any("track_id" in msg for msg in errors)

    def test_bad_content_hash_rejected(self, minimal_doc: dict) -> None:
        doc = dict(minimal_doc)
        doc["tracks"] = [dict(minimal_doc["tracks"][0])]
        doc["tracks"][0]["content_hash"] = "md5:abc"
        errors = validate_document(doc)
        assert errors
        assert any("content_hash" in msg for msg in errors)


@pytest.mark.requirement("OPEN-01")
class TestProvenanceEnum:
    """Spec section 4.1: `source` is enum-constrained."""

    def test_valid_source_accepted(self, minimal_doc: dict) -> None:
        doc = dict(minimal_doc)
        doc["tracks"] = [dict(minimal_doc["tracks"][0])]
        doc["tracks"][0]["bpm"] = {
            "value": 128.0, "source": "open-dj-tool",
            "modified_at": "2026-04-17T12:00:00Z"
        }
        assert validate_document(doc) == []

    def test_unknown_source_rejected(self, minimal_doc: dict) -> None:
        doc = dict(minimal_doc)
        doc["tracks"] = [dict(minimal_doc["tracks"][0])]
        doc["tracks"][0]["bpm"] = {
            "value": 128.0, "source": "some-vendor-not-in-enum",
            "modified_at": "2026-04-17T12:00:00Z"
        }
        assert validate_document(doc)
