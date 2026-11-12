"""[if] issue #11223 ships [then] FLAC parsers live in artifacts_flac.py under PY_FILE_LIMIT, [else stop]."""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import pytest

from apps.stems.artifact_types import WavMetadata
from apps.stems.artifacts import read_flac_metadata, read_stem_container_metadata
from apps.stems.artifacts_flac import read_flac_metadata as read_flac_direct
from scripts.quality_gate import CFG, REPO

pytestmark = pytest.mark.requirement("QUALITY-12057")

_ARTIFACTS = REPO / "apps/stems/artifacts.py"
_ARTIFACTS_FLAC = REPO / "apps/stems/artifacts_flac.py"
_BASELINE = REPO / "ops/quality/baseline.json"
_PY_LIMIT = CFG.PY_FILE_LIMIT


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _write_minimal_flac(path: Path, *, frames: int = 24, sample_rate: int = 44_100) -> None:
    wav = path.with_suffix(".wav")
    with wave.open(str(wav), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * frames * 2)
    ffmpeg = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), str(path)],
        capture_output=True,
    )
    if ffmpeg.returncode != 0:
        pytest.skip("ffmpeg required to synthesize FLAC stem fixtures")
    wav.unlink(missing_ok=True)


def test_stems_artifact_modules_under_py_file_limit() -> None:
    """[if] artifacts modules are measured [then] each is at or under PY_FILE_LIMIT, [else stop]."""
    assert _line_count(_ARTIFACTS) <= _PY_LIMIT, "artifacts.py exceeds PY_FILE_LIMIT"
    assert _line_count(_ARTIFACTS_FLAC) <= _PY_LIMIT, "artifacts_flac.py exceeds PY_FILE_LIMIT"


def test_parent_artifacts_exceeded_limit_before_flac_sibling() -> None:
    """[if] pre-split artifacts.py is inspected [then] it was over PY_FILE_LIMIT, [else stop]."""
    show = subprocess.run(
        ["git", "show", "323bd060f9^:apps/stems/artifacts.py"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    )
    if show.returncode != 0:
        pytest.skip("parent commit with monolithic artifacts.py not available")
    parent_lines = len(show.stdout.splitlines())
    assert parent_lines > _PY_LIMIT, "expected pre-split artifacts.py over PY_FILE_LIMIT"


def test_flac_metadata_parsers_match_container_dispatch(tmp_path: Path) -> None:
    """[if] a valid FLAC stem part exists [then] both metadata readers agree, [else stop]."""
    flac = tmp_path / "vocals.flac"
    _write_minimal_flac(flac)
    direct = read_flac_direct(flac)
    via_dispatch, suffix = read_stem_container_metadata(flac)
    via_shim = read_flac_metadata(flac)
    assert suffix == ".flac"
    assert via_dispatch == direct == via_shim
    assert isinstance(direct, WavMetadata)
    assert direct.sample_rate > 0
    assert direct.frame_count > 0
    assert direct.channels == 2


def test_baseline_records_artifacts_flac_burn_down() -> None:
    """[if] baseline burn_down loads [then] ISSUE_11223 names artifacts_flac split, [else stop]."""
    payload = json.loads(_BASELINE.read_text(encoding="utf-8"))
    row = payload["burn_down"]["ISSUE_11223_STEMS_ARTIFACTS_FLAC"]
    assert "artifacts_flac.py" in row["what"]
    assert "artifacts.py" in row["what"]
    paid = row["paid"]["file_size.over_limit_python"]
    assert "->" in paid


def test_quality_size_gate_runs() -> None:
    """[if] quality_gate size runs [then] stems artifact modules stay under PY_FILE_LIMIT, [else stop]."""
    proc = subprocess.run(
        [
            "uv",
            "run",
            "--no-project",
            "--quiet",
            "python",
            "-m",
            "scripts.quality_gate",
            "--only",
            "size",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert _line_count(_ARTIFACTS) <= _PY_LIMIT
    assert _line_count(_ARTIFACTS_FLAC) <= _PY_LIMIT
    # Inherited trunk size debt on some hosts must not hide a stems regression.
    if proc.returncode != 0 and "file_size.over_limit_python" in proc.stdout:
        assert _ARTIFACTS.name not in proc.stdout or "artifacts.py" not in proc.stdout
