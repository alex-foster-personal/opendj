"""Issue #13160: focused audit-quality CLI acceptance (clean, malformed, JSON).

[if] audit-quality CLI runs on fixtures [then] clean passes and defects surface, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.stems.audit_cli import run_audit_quality
from apps.stems.cli import main
from apps.stems.stem_set_audit import write_synthetic_stem_set

pytestmark = pytest.mark.requirement("STEM-10946")


def _compliant_data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    stems = data / "state" / "stems"
    stems.mkdir(parents=True)
    write_synthetic_stem_set(stems, "good", kind="compliant")
    return data


def _three_set_data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    stems = data / "state" / "stems"
    stems.mkdir(parents=True)
    write_synthetic_stem_set(stems, "good", kind="compliant")
    write_synthetic_stem_set(stems, "bad_codec", kind="flac_from_mp3")
    write_synthetic_stem_set(stems, "no_policy", kind="unrecorded")
    return data


def test_clean_store_text_output_is_quiet_and_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] manifest has no quality defects [then] exit 0 with only COMPLIANT rows, [else stop]."""
    data = _compliant_data_dir(tmp_path)
    rc = run_audit_quality(data_dir=data, json_output=False)
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    out = captured.out.strip().splitlines()
    assert out[-1] == "checked=1 compliant=1 violation=0 unknown=0"
    for line in out[:-1]:
        assert line.startswith("COMPLIANT ")
    body = captured.out + captured.err
    assert "VIOLATION" not in body
    assert "UNKNOWN" not in body
    assert "error:" not in body


def test_malformed_stem_artifact_surfaces_unknown_in_text_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a stem part file is garbage bytes [then] UNKNOWN with container reason, [else stop]."""
    data = _compliant_data_dir(tmp_path)
    stems = data / "state" / "stems"
    bundle = stems / "good"
    (bundle / "vocals.mp3").write_bytes(b"not-audio")
    capsys.readouterr()
    rc = run_audit_quality(data_dir=data, json_output=False)
    captured = capsys.readouterr()
    assert rc == 1
    assert "UNKNOWN" in captured.out
    assert "good" in captured.out
    assert "bytes are not a known audio container" in captured.out

    capsys.readouterr()
    rc_json = run_audit_quality(data_dir=data, json_output=True)
    json_captured = capsys.readouterr()
    assert rc_json == 1
    doc = json.loads(json_captured.out)
    assert doc["violation"] == 0
    assert doc["unknown"] >= 1
    good_rows = [r for r in doc["rows"] if r["stable_id"] == "good"]
    assert len(good_rows) == 1
    assert good_rows[0]["status"] == "UNKNOWN"


def test_json_output_is_a_single_document_with_counts_and_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] --json is requested [then] one JSON doc has counts and rows, [else stop]."""
    data = _three_set_data(tmp_path)
    rc = main(["audit-quality", "--data-dir", str(data), "--json"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == ""
    doc = json.loads(captured.out)
    assert set(doc.keys()) == {"checked", "compliant", "violation", "unknown", "rows"}
    assert doc["checked"] == 3
    assert doc["compliant"] == 1
    assert doc["violation"] == 1
    assert doc["unknown"] == 1
    for row in doc["rows"]:
        assert {"stable_id", "status", "axis", "reason"} <= set(row)
