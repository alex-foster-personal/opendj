"""STEM-10946: stems audit-quality CLI and classify_stem_set.

[if] audit-quality runs on a data dir [then] stem-sets classify from measured bytes, [else stop].
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared.paths import PROJECT_ROOT
from apps.stems import stem_size_policy as pol
from apps.stems.cli import main
from apps.stems.stem_set_audit import (
    AuditError,
    audit_store,
    measure_part,
    run_audit,
    run_self_test,
    synthetic_cbr_mp3_bytes,
    write_synthetic_stem_set,
)
from apps.stems.stem_size_policy import (
    AUDIT_COMPLIANT,
    AUDIT_UNKNOWN,
    AUDIT_VIOLATION,
    MeasuredPart,
    RecordedSource,
    classify_stem_set,
)

pytestmark = pytest.mark.requirement("STEM-10946")


def _mp3_part(name: str, kbps: float, size: int = 10_000) -> MeasuredPart:
    return MeasuredPart(name, "mp3", size, kbps)


def test_synthetic_mp3_bytes_measure_at_their_cbr(tmp_path: Path) -> None:
    """[if] synthetic CBR MP3 bytes [then] measure_part reports kbps within 2% of target."""
    path = tmp_path / "vocals.mp3"
    path.write_bytes(synthetic_cbr_mp3_bytes(80, 2.0))
    part = measure_part("vocals", path)
    assert part.codec == "mp3"
    assert part.kbps is not None
    assert abs(part.kbps - 80) <= 80 * 0.02


def test_measure_part_reads_flac_magic_not_the_extension(tmp_path: Path) -> None:
    """[if] FLAC magic is saved as vocals.mp3 [then] codec is flac."""
    path = tmp_path / "vocals.mp3"
    path.write_bytes(b"fLaC" + bytes(100))
    part = measure_part("vocals", path)
    assert part.codec == "flac"


def test_classify_flac_from_mp3_is_codec_violation() -> None:
    """[if] every part is flac from a .mp3 source [then] VIOLATION codec."""
    parts = [MeasuredPart(n, "flac", 1000, None) for n in ("bass", "drums", "other", "vocals")]
    v = classify_stem_set(
        layout=pol.WHOLE_SET_LAYOUT,
        parts=parts,
        source=RecordedSource(".mp3", 80_000, 320.0),
    )
    assert v.status == AUDIT_VIOLATION
    assert v.axis == pol.AXIS_CODEC


def test_classify_no_recorded_source_quality_is_unknown_never_compliant() -> None:
    """[if] source_bytes is missing [then] UNKNOWN."""
    parts = [_mp3_part(n, 80.0) for n in ("bass", "drums", "other", "vocals")]
    v = classify_stem_set(
        layout=pol.WHOLE_SET_LAYOUT,
        parts=parts,
        source=RecordedSource(".mp3", None, 320.0),
    )
    assert v.status == AUDIT_UNKNOWN


def test_classify_part_over_target_is_bitrate_violation() -> None:
    """[if] vocals measures 320 kbps on a 320 source [then] bitrate VIOLATION."""
    parts = [
        _mp3_part("bass", 96.0),
        _mp3_part("drums", 96.0),
        _mp3_part("other", 80.0),
        _mp3_part("vocals", 320.0),
    ]
    v = classify_stem_set(
        layout=pol.WHOLE_SET_LAYOUT,
        parts=parts,
        source=RecordedSource(".mp3", 80_000, 320.0),
    )
    assert v.status == AUDIT_VIOLATION
    assert v.axis == pol.AXIS_BITRATE


def test_classify_floor_forced_set_over_source_is_compliant() -> None:
    """[if] a 128 kbps source has parts at their floors [then] COMPLIANT."""
    parts = [
        _mp3_part("bass", 96.0),
        _mp3_part("drums", 96.0),
        _mp3_part("other", 64.0),
        _mp3_part("vocals", 64.0),
    ]
    source_bytes = 16_000
    v = classify_stem_set(
        layout=pol.WHOLE_SET_LAYOUT,
        parts=parts,
        source=RecordedSource(".mp3", source_bytes, 128.0),
    )
    assert v.status == AUDIT_COMPLIANT


def test_classify_roformer_layout_is_unknown() -> None:
    """[if] layout is roformer2 [then] UNKNOWN."""
    parts = [_mp3_part(n, 80.0) for n in ("bass", "drums", "other", "vocals")]
    v = classify_stem_set(
        layout="roformer2",
        parts=parts,
        source=RecordedSource(".mp3", 80_000, 320.0),
    )
    assert v.status == AUDIT_UNKNOWN


def test_manifest_claims_are_ignored(tmp_path: Path) -> None:
    """[if] bytes are FLAC but manifest says mp3 [then] still codec VIOLATION."""
    root = tmp_path / "stems"
    bundle = write_synthetic_stem_set(root, "lying", kind="flac_from_mp3")
    manifest_path = bundle / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["codec"] = "mp3"
    raw["size_policy"] = {"source_bytes": 80_000, "source_kbps": 320.0, "set_ratio": 0.9}
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    row = audit_store(root).rows[0]
    assert row.status == AUDIT_VIOLATION
    assert row.axis == pol.AXIS_CODEC


def _three_set_data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    stems = data / "state" / "stems"
    stems.mkdir(parents=True)
    write_synthetic_stem_set(stems, "good", kind="compliant")
    write_synthetic_stem_set(stems, "bad_codec", kind="flac_from_mp3")
    write_synthetic_stem_set(stems, "no_policy", kind="unrecorded")
    return data


def test_audit_store_one_of_each_counts_and_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] the store holds compliant, violation and unknown sets [then] summary counts match."""
    data = _three_set_data(tmp_path)
    rc = main(["audit-quality", "--data-dir", str(data)])
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 1
    assert out[-1] == "checked=3 compliant=1 violation=1 unknown=1"


def test_audit_store_all_compliant_exits_zero(tmp_path: Path) -> None:
    """[if] every set is compliant [then] exit 0."""
    data = tmp_path / "data"
    stems = data / "state" / "stems"
    stems.mkdir(parents=True)
    write_synthetic_stem_set(stems, "a", kind="compliant")
    write_synthetic_stem_set(stems, "b", kind="compliant")
    assert main(["audit-quality", "--data-dir", str(data)]) == 0


def test_empty_store_is_an_error_not_a_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] the stems store is empty or missing [then] exit 2 with error on stderr."""
    data = tmp_path / "data"
    empty = data / "state" / "stems"
    empty.mkdir(parents=True)
    rc = main(["audit-quality", "--data-dir", str(data)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "error:" in err
    missing = tmp_path / "nodata"
    assert main(["audit-quality", "--data-dir", str(missing)]) == 2


def test_json_has_counts_and_one_row_per_set(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] --json [then] counts and per-set rows are present."""
    data = _three_set_data(tmp_path)
    rc = main(["audit-quality", "--data-dir", str(data), "--json"])
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["checked"] == 3
    assert doc["compliant"] == 1
    assert doc["violation"] == 1
    assert doc["unknown"] == 1
    assert len(doc["rows"]) == 3
    for row in doc["rows"]:
        assert {"stable_id", "status", "axis", "reason"} <= set(row)


def test_self_test_failure_stops_before_the_store_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the self-test would fail [then] the real store is never read."""
    missing = tmp_path / "no_store"

    def broken_self_test() -> None:
        raise AuditError("self-test failed: expected flac=VIOLATION")

    monkeypatch.setattr("apps.stems.stem_set_audit.run_self_test", broken_self_test)

    with pytest.raises(AuditError, match="self-test"):
        run_audit(missing)
    assert main(["audit-quality", "--data-dir", str(missing)]) == 2


def test_runtime_self_test_passes() -> None:
    """[if] built-in fixtures [then] self-test accepts VIOLATION and COMPLIANT."""
    run_self_test()


def test_module_entrypoint_runs(tmp_path: Path) -> None:
    """[if] python -m apps.stems.cli audit-quality --json [then] counts match."""
    data = _three_set_data(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.stems.cli",
            "audit-quality",
            "--data-dir",
            str(data),
            "--json",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    doc = json.loads(proc.stdout)
    assert doc == {"checked": 3, "compliant": 1, "violation": 1, "unknown": 1, "rows": doc["rows"]}
