"""The payload stages odj-output-probe for Electron CoreAudio listing (issue #6864).

[if] the payload manifest is written [then] it records output_probe path and sha256, [else stop].
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts import payload_output_probe
from scripts.build_engine_payload import (
    AUDIO_ENGINE_RELATIVE,
    PayloadBuildError,
    PayloadIdentity,
    PayloadReport,
    StagedPayload,
    build_manifest,
    sha256_file,
    stage_output_probe,
)
from scripts.payload_output_probe import OUTPUT_PROBE_RELATIVE

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_output_probe_relative_path() -> None:
    assert OUTPUT_PROBE_RELATIVE == "bin/odj-output-probe"


def _minimal_manifest_fixture(tmp_path: Path) -> tuple[Path, StagedPayload]:
    payload_dir = tmp_path / "payload"
    build_dir = payload_dir / "app" / "apps" / "webui" / "frontend" / "build"
    for sub in ("runtime", "pylib"):
        (payload_dir / sub).mkdir(parents=True)
    build_dir.mkdir(parents=True)
    (build_dir / "index.html").write_text("<html></html>")
    stretch = build_dir / "stretch.wasm"
    stretch.write_bytes(b"\0")
    engine_bin = payload_dir / AUDIO_ENGINE_RELATIVE
    engine_bin.parent.mkdir(parents=True, exist_ok=True)
    engine_bin.write_bytes(b"engine-stub")
    output_probe = payload_dir / OUTPUT_PROBE_RELATIVE
    output_probe.parent.mkdir(parents=True, exist_ok=True)
    output_probe.write_bytes(b"probe-stub")
    staged = StagedPayload(
        runtime_source=tmp_path,
        python_version="3.11",
        pruned=[],
        requirements=[],
        orphaned=[],
        stretch_asset=stretch,
        waveform_wheel_name="",
        waveform_wheel_sha256="",
        audio_engine_hello={"engine": "0.0.0", "protocol": 1, "device": True},
    )
    return payload_dir, staged


def test_build_manifest_records_output_probe_path_and_sha256(tmp_path: Path) -> None:
    """[if] build_manifest runs [then] output_probe matches the staged helper digest, [else stop]."""
    payload_dir, staged = _minimal_manifest_fixture(tmp_path)
    probe_path = payload_dir / OUTPUT_PROBE_RELATIVE
    manifest = build_manifest(
        repo_root=REPO_ROOT,
        payload_dir=payload_dir,
        identity_input=PayloadIdentity(
            label=None,
            product_name="Open DJ",
            identifier="com.example.opendj",
            app_version="0.0.0",
            engine_version="0.0.0",
        ),
        staged=staged,
        report=PayloadReport(),
    )
    block = manifest["output_probe"]
    assert block == {
        "path": OUTPUT_PROBE_RELATIVE,
        "sha256": sha256_file(probe_path),
    }


@pytest.mark.skipif(shutil.which("cargo") is None, reason="UNAVAILABLE: cargo is not on PATH")
def test_stage_output_probe_copies_a_release_binary(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    stage_output_probe(REPO_ROOT, payload)
    staged = payload / OUTPUT_PROBE_RELATIVE
    assert staged.is_file()
    assert staged.stat().st_mode & 0o111
    assert staged.stat().st_size > 0


def test_stage_output_probe_without_cargo_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """[if] cargo is absent [then] staging refuses instead of skipping at call sites, [else stop]."""
    monkeypatch.setattr(payload_output_probe.shutil, "which", lambda _name: None)
    payload = tmp_path / "payload"
    payload.mkdir()
    with pytest.raises(PayloadBuildError, match="cargo is not on PATH"):
        stage_output_probe(REPO_ROOT, payload)
