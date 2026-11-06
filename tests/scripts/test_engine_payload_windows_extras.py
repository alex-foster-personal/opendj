"""Windows payload extras: beatgrid runner, Google OAuth, telemetry DSN, third-party licenses.

Real files in tmp_path; the secret-bearing inputs come from an explicit env mapping and PATH is
emptied so a live Doppler login on the build host can never green a missing-input case.

- if the Windows export keeps +cu126 on torch, the beatgrid runner ships a 2.6 GB CUDA wheel it never uses -> broken.
- if the export rewrite touches a package other than torch/torchaudio, the runner closure drifts from the lock -> broken.
- if the beatgrid launcher extends PYTHONPATH instead of replacing it, app/ or pylib/ can shadow the runner pins -> broken.
- if the beatgrid launcher drops "%*", the engine's runner arguments never arrive -> broken.
- if the beatgrid launcher clears PYTHONHOME, it diverges from the POSIX runner -> broken.
- if a release build without OAuth or telemetry inputs succeeds, a packaged install signs in to nothing and reports nothing -> broken.
- if a dev build without those inputs does not name each one under not_staged, the gap is silent -> broken.
- if the staged OAuth/telemetry files land anywhere but the macOS-identical relative paths, the launcher contract diverges -> broken.
- if the engine launcher omits MDT_BEATGRID_RUNNER_PYTHON, MDT_BEATGRID_WEIGHTS or OPENDJ_BUNDLED_TELEMETRY, the engine cannot find them -> broken.
- if the runner verification passes without the checkpoint, the weights notice or the site, zero is mistaken for staged -> broken.
- if the runtime licence lookup only knows the macOS runtime/lib layout, a Windows payload cannot be inventoried -> broken.
- if the reviewed-license pin hashes CRLF bytes, every Windows checkout fails the inventory on a correct file -> broken.
- if applying extras to a manifest leaves a staged piece under not_staged, the manifest contradicts the payload -> broken.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_engine_payload import PayloadBuildError
from scripts.build_engine_payload_win import NOT_STAGED, standing_not_staged
from scripts.license_bundled import runtime_license_source
from scripts.license_mirrors import REVIEWED_LICENSE_TEXTS
from scripts.license_model import Component, LicenseInventoryError
from scripts.payload_beatgrid import CHECKPOINT_LICENSE_RELATIVE, CHECKPOINT_RELATIVE, RUNNER_SITE_RELATIVE
from scripts.payload_google_oauth import BUNDLED_RELATIVE as OAUTH_RELATIVE
from scripts.payload_launchers_win import write_windows_launchers
from scripts.payload_telemetry import BUNDLED_RELATIVE as TELEMETRY_RELATIVE
from scripts.payload_telemetry import FRONTEND_DSN_ENV, SHIP_DSN_ENV
from scripts.payload_windows_extras import (
    BEATGRID_LAUNCHER_RELATIVE,
    ExtrasResult,
    apply_extras_to_manifest,
    stage_baked_inputs,
    strip_cuda_local_versions,
    verify_beatgrid_runner_files,
    write_beatgrid_launcher,
)
from scripts.third_party_licenses import attach_reviewed_license_texts

REPO_ROOT = Path(__file__).resolve().parents[2]
OAUTH_ENV = {"OPENDJ_GOOGLE_OAUTH_CLIENT_ID": "test-client-id", "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET": "test-secret"}
TELEMETRY_ENV = {
    SHIP_DSN_ENV: "https://public@o0.ingest.de.sentry.io/42",
    FRONTEND_DSN_ENV: "https://public@o0.ingest.de.sentry.io/43",
}
FULL_ENV = {**OAUTH_ENV, **TELEMETRY_ENV}


@pytest.fixture(autouse=True)
def no_doppler_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")


def test_strip_cuda_local_versions_rewrites_only_torch_and_torchaudio() -> None:
    export = (
        "numpy==1.26.4\n"
        "torch==2.14.0 ; sys_platform != 'linux' and sys_platform != 'win32'\n"
        "    # via beat-this\n"
        "torch==2.14.0+cpu ; sys_platform == 'linux'\n"
        "torch==2.14.0+cu126 ; sys_platform == 'win32'\n"
        "torchaudio==2.11.0+cu126 ; sys_platform == 'win32'\n"
        "somepkg==1.0+cu126\n"
    )
    rewritten = strip_cuda_local_versions(export).splitlines()
    assert "torch==2.14.0 ; sys_platform == 'win32'" in rewritten
    assert "torchaudio==2.11.0 ; sys_platform == 'win32'" in rewritten
    assert "torch==2.14.0 ; sys_platform == 'linux'" in rewritten
    assert "numpy==1.26.4" in rewritten
    assert "somepkg==1.0+cu126" in rewritten
    assert "    # via beat-this" in rewritten


def test_beatgrid_launcher_replaces_pythonpath_resolves_relative_and_forwards_args(tmp_path: Path) -> None:
    launcher = write_beatgrid_launcher(tmp_path)
    assert launcher == tmp_path / BEATGRID_LAUNCHER_RELATIVE
    text = launcher.read_bytes().decode("ascii")
    assert '"%~dp0.."' in text
    assert 'set "PYTHONPATH=%payload%\\runners\\beatgrid\\site"\r\n' in text
    assert "%payload%\\app" not in text and "%payload%\\pylib" not in text
    assert '"%payload%\\runtime\\python.exe" %*' in text
    assert "PYTHONHOME" not in text


def test_release_build_without_inputs_fails_naming_both_missing_inputs(tmp_path: Path) -> None:
    with pytest.raises(PayloadBuildError, match="Google OAuth client id"):
        stage_baked_inputs(tmp_path, allow_missing=False, env={})
    assert not (tmp_path / OAUTH_RELATIVE).exists()
    with pytest.raises(PayloadBuildError, match=SHIP_DSN_ENV):
        stage_baked_inputs(tmp_path, allow_missing=False, env=OAUTH_ENV)


def test_dev_build_without_inputs_records_each_gap_under_not_staged(tmp_path: Path) -> None:
    not_staged = stage_baked_inputs(tmp_path, allow_missing=True, env={})
    assert set(not_staged) == {"google_oauth", "telemetry"}
    assert "client id" in not_staged["google_oauth"] and SHIP_DSN_ENV in not_staged["telemetry"]
    assert not (tmp_path / OAUTH_RELATIVE).exists() and not (tmp_path / TELEMETRY_RELATIVE).exists()


def test_dev_build_stages_what_it_has_and_names_only_what_is_missing(tmp_path: Path) -> None:
    not_staged = stage_baked_inputs(tmp_path, allow_missing=True, env=OAUTH_ENV)
    assert set(not_staged) == {"telemetry"}
    assert json.loads((tmp_path / OAUTH_RELATIVE).read_text(encoding="utf-8"))["client_id"] == "test-client-id"


def test_inputs_land_at_the_macos_relative_paths(tmp_path: Path) -> None:
    assert stage_baked_inputs(tmp_path, allow_missing=False, env=FULL_ENV) == {}
    assert OAUTH_RELATIVE.as_posix() == "app/apps/shared/bundled_google_oauth.json"
    assert TELEMETRY_RELATIVE.as_posix() == "telemetry.json"
    assert (tmp_path / OAUTH_RELATIVE).is_file() and (tmp_path / TELEMETRY_RELATIVE).is_file()


def test_engine_launcher_exports_beatgrid_paths_and_the_telemetry_bundle_when_present(tmp_path: Path) -> None:
    engine, cli, _python = write_windows_launchers(tmp_path)
    for launcher in (engine, cli):
        text = launcher.read_bytes().decode("ascii")
        assert 'set "MDT_BEATGRID_RUNNER_PYTHON=%payload%\\bin\\opendj-beatgrid-python.cmd"' in text
        assert 'set "MDT_BEATGRID_WEIGHTS=%payload%\\models\\beatgrid\\beat_this-final0.ckpt"' in text
        assert 'set "OPENDJ_BUNDLED_TELEMETRY=%payload%\\telemetry.json"' in text
        assert "PYTHONHOME" not in text
        assert "if not defined MDT_LIBRARY_MODE" in text


def make_runner_files(payload: Path) -> None:
    (payload / RUNNER_SITE_RELATIVE).mkdir(parents=True)
    (payload / CHECKPOINT_RELATIVE).parent.mkdir(parents=True)
    (payload / CHECKPOINT_RELATIVE).write_bytes(b"not the real checkpoint")
    (payload / CHECKPOINT_LICENSE_RELATIVE).write_text("MIT License\n", encoding="utf-8")
    write_beatgrid_launcher(payload)


@pytest.mark.parametrize("missing", ["site", "checkpoint", "launcher"])
def test_runner_file_verification_names_each_missing_piece(tmp_path: Path, missing: str) -> None:
    make_runner_files(tmp_path)
    target = {
        "site": tmp_path / RUNNER_SITE_RELATIVE,
        "checkpoint": tmp_path / CHECKPOINT_RELATIVE,
        "launcher": tmp_path / BEATGRID_LAUNCHER_RELATIVE,
    }[missing]
    if target.is_dir():
        target.rmdir()
    else:
        target.unlink()
    with pytest.raises(PayloadBuildError, match="missing"):
        verify_beatgrid_runner_files(tmp_path)


def test_runner_file_verification_refuses_a_checkpoint_with_the_wrong_digest(tmp_path: Path) -> None:
    make_runner_files(tmp_path)
    with pytest.raises(PayloadBuildError, match="sha256"):
        verify_beatgrid_runner_files(tmp_path)


def test_runtime_license_lookup_knows_the_windows_layout(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "LICENSE.txt").write_text("PSF\n", encoding="utf-8")
    (runtime / "python311.dll").write_bytes(b"MZ")
    path, version = runtime_license_source(tmp_path)
    assert path == runtime / "LICENSE.txt" and version == "python3.11"


def test_runtime_license_lookup_knows_the_macos_layout(tmp_path: Path) -> None:
    lib = tmp_path / "runtime" / "lib" / "python3.11"
    lib.mkdir(parents=True)
    (lib / "LICENSE.txt").write_text("PSF\n", encoding="utf-8")
    path, version = runtime_license_source(tmp_path)
    assert path == lib / "LICENSE.txt" and version == "python3.11"


def test_runtime_license_lookup_fails_when_neither_layout_has_one(tmp_path: Path) -> None:
    (tmp_path / "runtime").mkdir()
    with pytest.raises(LicenseInventoryError, match=r"LICENSE.txt"):
        runtime_license_source(tmp_path)


def test_applying_extras_moves_staged_pieces_out_of_not_staged() -> None:
    manifest = {"not_staged": dict(NOT_STAGED, google_oauth="x", telemetry="y")}
    result = ExtrasResult(
        manifest_blocks={"beatgrid_runner": {"site": "s"}, "third_party_licenses": {"components": 3}},
        not_staged={"telemetry": "no DSN"},
    )
    applied = apply_extras_to_manifest(manifest, result, remaining_gaps=standing_not_staged())
    assert applied["beatgrid_runner"] == {"site": "s"} and applied["third_party_licenses"] == {"components": 3}
    assert set(applied["not_staged"]) == {"ffmpeg", "waveform_native", "telemetry"}
    assert applied["not_staged"]["telemetry"] == "no DSN"
    assert "authenticode" not in applied["not_staged"]


def test_reviewed_license_text_hash_survives_a_crlf_checkout(tmp_path: Path) -> None:
    """A Windows checkout rewrites docs/legal/* to CRLF; the pin is on the committed LF bytes."""
    entry = REVIEWED_LICENSE_TEXTS[0]
    committed = (REPO_ROOT / entry.text_relative).read_bytes().replace(b"\r\n", b"\n")
    crlf_copy = tmp_path / entry.text_relative
    crlf_copy.parent.mkdir(parents=True)
    crlf_copy.write_bytes(committed.replace(b"\n", b"\r\n"))
    textless = Component(entry.ecosystem, entry.name, entry.version, "", "", [])
    (attached,) = attach_reviewed_license_texts(tmp_path, [textless])
    assert any(text.strip() for _, text in attached.license_texts)


def test_reviewed_license_text_that_is_not_the_reviewed_text_is_still_refused(tmp_path: Path) -> None:
    entry = REVIEWED_LICENSE_TEXTS[0]
    tampered = tmp_path / entry.text_relative
    tampered.parent.mkdir(parents=True)
    tampered.write_text("not the reviewed text\n", encoding="utf-8")
    textless = Component(entry.ecosystem, entry.name, entry.version, "", "", [])
    with pytest.raises(LicenseInventoryError, match="not the reviewed text"):
        attach_reviewed_license_texts(tmp_path, [textless])
