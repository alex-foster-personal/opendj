"""The payload carries the CoreAudio output-health helper on macOS (ELECTRON-6272).

[if] the payload is built on darwin [then] cargo builds odj-output-probe and it is staged executable at bin/odj-output-probe, [else stop].
[if] the payload is built on Linux or Windows [then] no cargo build runs and the skip names the macOS-only reason, [else stop].
[if] the staged file does not answer an empty argv with the helper's usage error [then] the build stops, [else stop].
[if] the helper's crate lives under or depends on the Tauri shell [then] deleting apps/desktop/src-tauri breaks the Electron payload, [else stop].

Without the helper the packed Electron app answers output health ``unknown``
forever, because output-health.ts finds nothing at that path.

-Claude
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from scripts import payload_output_probe
from scripts.build_engine_payload import PayloadBuildError
from scripts.payload_output_probe import (
    OUTPUT_PROBE_BIN,
    OUTPUT_PROBE_CRATE,
    OUTPUT_PROBE_RELATIVE,
    USAGE_MARKER,
    stage_output_probe,
    verify_output_probe,
)

pytestmark = pytest.mark.requirement("ELECTRON-6272")

REPO_ROOT = Path(__file__).resolve().parents[2]
POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="the gate inputs are /bin/sh scripts with an executable bit"
)

HELPER_SCRIPT = f"#!/bin/sh\nprintf '{USAGE_MARKER} <probe|switch>\\n' >&2\nexit 2\n"
NOT_THE_HELPER = "#!/bin/sh\necho hello\nexit 0\n"


@pytest.mark.skipif(sys.platform != "darwin", reason="the helper is a CoreAudio binary: only a macOS host builds it")
def test_darwin_builds_and_stages_the_real_helper(tmp_path: Path) -> None:
    """The REAL cargo build of the real crate, staged and gated (discussion on #6272).

    No stand-in: on a macOS host this compiles odj-output-probe and runs the
    staged binary through the usage gate. Linux CI runners cannot build it."""
    payload = tmp_path / "payload"
    block = stage_output_probe(REPO_ROOT, payload, "darwin")
    assert block == {"staged": True, "path": OUTPUT_PROBE_RELATIVE}
    staged = payload / OUTPUT_PROBE_RELATIVE
    assert staged.is_file() and staged.stat().st_mode & 0o111 == 0o111
    verify_output_probe(staged)


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_off_darwin_the_helper_is_skipped_with_the_reason(
    platform: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real production skip path: no build output anywhere, and the reason logged."""
    repo = tmp_path / "repo"
    block = stage_output_probe(repo, tmp_path / "payload", platform)
    assert block["staged"] is False
    reason = str(block["reason"])
    assert "macOS-only" in reason and platform in reason
    assert not (tmp_path / "payload").exists(), "nothing staged"
    assert not (repo / OUTPUT_PROBE_CRATE / "target").exists(), "a non-darwin build must not run cargo"
    assert f"[SKIP] {reason}" in capsys.readouterr().err


def test_cargo_runs_through_the_path_which_resolved(tmp_path: Path) -> None:
    """The validated program is the launched one: argv[0] is the resolved path, not a bare name."""
    resolved = "/opt/toolchain/bin/cargo"
    cmd = payload_output_probe.cargo_command(resolved, tmp_path / "crate", tmp_path / "target")
    assert cmd[0] == resolved
    assert cmd[1:5] == ["build", "--release", "--locked", "--bin"] and cmd[5] == OUTPUT_PROBE_BIN
    assert Path(cmd[cmd.index("--manifest-path") + 1]) == tmp_path / "crate" / "Cargo.toml"


def test_no_cargo_on_path_stops_a_darwin_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real empty PATH (environment configuration), not a patched lookup."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(PayloadBuildError, match="cargo is not on PATH"):
        stage_output_probe(tmp_path / "repo", tmp_path / "payload", "darwin")


@pytest.mark.skipif(shutil.which("cargo") is None, reason="needs a real cargo on this host")
def test_a_failed_real_cargo_build_stops_the_build(tmp_path: Path) -> None:
    """The real cargo, run against a crate path with no Cargo.toml, really fails."""
    with pytest.raises(PayloadBuildError, match=f"cargo build of {OUTPUT_PROBE_BIN} failed"):
        stage_output_probe(tmp_path / "repo", tmp_path / "payload", "darwin")
    assert not (tmp_path / "payload" / OUTPUT_PROBE_RELATIVE).exists()


@POSIX_ONLY
def test_a_file_that_is_not_the_helper_fails_the_gate(tmp_path: Path) -> None:
    binary = tmp_path / OUTPUT_PROBE_BIN
    binary.write_text(NOT_THE_HELPER, encoding="utf-8")
    binary.chmod(0o755)
    with pytest.raises(PayloadBuildError, match="is not the output probe helper"):
        verify_output_probe(binary)


@POSIX_ONLY
def test_the_verify_accepts_the_helpers_usage_error(tmp_path: Path) -> None:
    """Control for the refusal above: the real contract must pass."""
    binary = tmp_path / OUTPUT_PROBE_BIN
    binary.write_text(HELPER_SCRIPT, encoding="utf-8")
    binary.chmod(0o755)
    verify_output_probe(binary)


def test_the_gate_reads_what_the_real_helper_prints() -> None:
    """The usage marker and exit code are the Rust helper's, not only the fake's."""
    source = (REPO_ROOT / OUTPUT_PROBE_CRATE / "src/main.rs").read_text(encoding="utf-8")
    usage = re.search(r'const USAGE: &str = "([^"]*)"', source)
    assert usage is not None and usage.group(1).startswith(USAGE_MARKER)
    assert f"ExitCode::from({payload_output_probe.USAGE_EXIT_CODE})" in source
    cargo = (REPO_ROOT / OUTPUT_PROBE_CRATE / "Cargo.toml").read_text(encoding="utf-8")
    assert f'name = "{OUTPUT_PROBE_BIN}"' in cargo


def test_the_electron_shell_looks_where_the_payload_stages() -> None:
    shell = (REPO_ROOT / "apps/desktop/electron/src/output-health.ts").read_text(encoding="utf-8")
    assert f"export const OUTPUT_PROBE_HELPER = '{OUTPUT_PROBE_RELATIVE}';" in shell


def test_the_helper_crate_builds_without_the_tauri_shell() -> None:
    """The Electron payload must survive deleting apps/desktop/src-tauri (Tauri deletion blocker)."""
    assert not OUTPUT_PROBE_CRATE.startswith("apps/desktop/src-tauri")
    cargo = (REPO_ROOT / OUTPUT_PROBE_CRATE / "Cargo.toml").read_text(encoding="utf-8")
    assert re.search(r"^\s*tauri", cargo, re.MULTILINE) is None, "the helper crate must not depend on tauri"
    assert re.search(r'path\s*=\s*"[^"]*src-tauri', cargo) is None, "no path dependency into the Tauri crate"
    assert not (REPO_ROOT / OUTPUT_PROBE_CRATE / "build.rs").exists(), "no tauri-build step"


def _artifact(name: str, executable: str | None) -> str:
    return json.dumps({"reason": "compiler-artifact", "target": {"name": name}, "executable": executable})


def test_the_staged_file_is_the_one_cargo_reports_under_a_target_triple() -> None:
    """With build.target set cargo writes target/<triple>/release; its own message is the source of truth."""
    triple_path = "/repo/apps/desktop/native/odj-output-probe/target/aarch64-apple-darwin/release/odj-output-probe"
    stdout = "\n".join(
        [
            _artifact("serde_json", None),
            _artifact("odj-audio", "/elsewhere/odj-audio"),
            _artifact(OUTPUT_PROBE_BIN, triple_path),
            json.dumps({"reason": "build-finished", "success": True}),
        ]
    )
    assert payload_output_probe.built_executable(stdout) == Path(triple_path)
    assert "--message-format=json-render-diagnostics" in payload_output_probe.cargo_command(
        "cargo", Path("c"), Path("t")
    )


def test_no_reported_executable_stops_the_build() -> None:
    stdout = _artifact(OUTPUT_PROBE_BIN, None) + "\n" + json.dumps({"reason": "build-finished", "success": True})
    with pytest.raises(PayloadBuildError, match="reported no executable"):
        payload_output_probe.built_executable(stdout)
