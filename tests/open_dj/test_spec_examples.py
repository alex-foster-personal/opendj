"""Regression tests for normative examples in the published open-dj v0.2 spec.

[if] Appendix A drifts from v0.2 [then] schema validation fails, [else stop].
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.open_dj.validate import validate_document

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC_V02 = _REPO_ROOT / "open-dj" / "spec" / "v0.2" / "open-dj.md"

_APPENDIX_A_HEADING = "## Appendix A — Canonical example"
_APPENDIX_B_HEADING = "## Appendix B —"
_SECTION_11_HEADING = "## 11. Versioning"
_SECTION_12_HEADING = "## 12. License and governance"

pytestmark = pytest.mark.requirement("OPEN-03a")


def _read_spec() -> str:
    return _SPEC_V02.read_text(encoding="utf-8")


def _extract_appendix_a_json(spec_text: str) -> str:
    start = spec_text.find(_APPENDIX_A_HEADING)
    assert start != -1, f"missing heading: {_APPENDIX_A_HEADING}"
    after_heading = spec_text[start + len(_APPENDIX_A_HEADING) :]
    appendix_b = after_heading.find(_APPENDIX_B_HEADING)
    assert appendix_b != -1, f"missing heading: {_APPENDIX_B_HEADING}"
    appendix_a = after_heading[:appendix_b]
    fence_open = appendix_a.find("```json")
    assert fence_open != -1, "Appendix A has no ```json fenced block"
    body_start = fence_open + len("```json")
    if appendix_a[body_start : body_start + 1] == "\n":
        body_start += 1
    fence_close = appendix_a.find("```", body_start)
    assert fence_close != -1, "Appendix A json fence is not closed"
    return appendix_a[body_start:fence_close].strip()


def test_appendix_a_example_validates_against_v0_2_schema() -> None:
    raw = _extract_appendix_a_json(_read_spec())
    document = json.loads(raw)
    assert isinstance(document, dict)
    assert document.get("schema_version") == "0.2"
    errors = validate_document(document)
    assert errors == [], f"Appendix A validation errors: {errors}"


def test_versioning_section_declares_v0_2() -> None:
    spec_text = _read_spec()
    start = spec_text.find(_SECTION_11_HEADING)
    assert start != -1, f"missing heading: {_SECTION_11_HEADING}"
    after = spec_text[start + len(_SECTION_11_HEADING) :]
    end = after.find(_SECTION_12_HEADING)
    assert end != -1, f"missing heading: {_SECTION_12_HEADING}"
    section = after[:end]
    assert 'schema_version: "0.2"' in section
    assert 'schema_version: "0.1"' not in section
