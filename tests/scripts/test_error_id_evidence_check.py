"""Unit tests for scripts/error_id_evidence_check.py (issue #2325).

[if] a cited id has no FEATURE EVIDENCE [then] the check exits 1, [else stop].
"""

from __future__ import annotations

import pytest

from apps.shared.telemetry.error_id import ERROR_ID_HEX_LEN, ERROR_ID_PREFIX
from scripts import error_id_evidence_check as eiec
from scripts.error_id_evidence_check import check, citations, main, problems

_SYNTHETIC = ERROR_ID_PREFIX + "deadbeef0001"
_ZERO = ERROR_ID_PREFIX + "0" * ERROR_ID_HEX_LEN


pytestmark = pytest.mark.requirement("OBS-9280")


def test_empty_body_passes() -> None:
    v = check("")
    assert v.ok
    assert problems("") == []


def test_body_without_eid_passes() -> None:
    body = "No error citation here.\n"
    assert check(body).ok


def test_citation_without_evidence_label_fails() -> None:
    body = f"Fix for {_SYNTHETIC}\n"
    assert not check(body).ok
    assert "no FEATURE EVIDENCE" in problems(body)[0].lower() or "fix evidence" in problems(body)[0].lower()


def test_inline_na_with_reason_passes() -> None:
    body = (
        f"{_SYNTHETIC}\n\n"
        "FEATURE EVIDENCE: N/A (docs and CI gate; nonvisual).\n"
    )
    assert check(body).ok


def test_heading_fix_evidence_with_trace_passes() -> None:
    body = (
        f"Row: {_SYNTHETIC}\n\n"
        "## FIX EVIDENCE\n"
        "Uploaded electron-trace.zip from Playwright.\n"
    )
    assert check(body).ok


def test_evidence_with_png_passes() -> None:
    body = (
        f"See {_SYNTHETIC}\n\n"
        "FEATURE EVIDENCE:\n"
        "artifact: out/screenshot.png\n"
    )
    assert check(body).ok


def test_bare_na_fails() -> None:
    body = (
        f"{_SYNTHETIC}\n\n"
        "FEATURE EVIDENCE: N/A\n"
    )
    assert not check(body).ok


def test_label_but_no_reason_or_capture_fails() -> None:
    body = (
        f"{_SYNTHETIC}\n\n"
        "FEATURE EVIDENCE:\n"
        "something happened\n"
    )
    assert not check(body).ok


def test_all_zero_placeholder_no_evidence_passes() -> None:
    body = f"Example only {_ZERO} in prose.\n"
    assert check(body).ok
    assert citations(eiec.strip_html_comments(body)) == []


def test_bare_eid_prefix_passes() -> None:
    body = "Mention eid- in prose without hex tail.\n"
    assert check(body).ok


def test_citation_in_fenced_json_with_na_passes() -> None:
    body = (
        "```json\n"
        f'  "error_id": "{_SYNTHETIC}"\n'
        "```\n\n"
        "FEATURE EVIDENCE: N/A (docs only).\n"
    )
    assert check(body).ok


def test_citation_in_fenced_json_without_evidence_fails() -> None:
    body = (
        "```json\n"
        f'  "error_id": "{_SYNTHETIC}"\n'
        "```\n"
    )
    assert not check(body).ok


def test_id_in_html_comment_ignored() -> None:
    body = f"<!-- {_SYNTHETIC} hidden -->\nFEATURE EVIDENCE: N/A (comment only).\n"
    assert check(body).ok


def test_cli_missing_file_exits_2(tmp_path) -> None:
    p = tmp_path / "missing.md"
    assert main(["error_id_evidence_check", "--body-file", str(p)]) == 2


def test_cli_missing_body_file_arg_exits_2() -> None:
    assert main(["error_id_evidence_check"]) == 2


def test_cli_passes(tmp_path) -> None:
    p = tmp_path / "b.md"
    p.write_text("", encoding="utf-8")
    assert main(["error_id_evidence_check", "--body-file", str(p)]) == 0


def test_cli_fail(tmp_path) -> None:
    p = tmp_path / "b.md"
    p.write_text(f"{_SYNTHETIC}\n", encoding="utf-8")
    assert main(["error_id_evidence_check", "--body-file", str(p)]) == 1
