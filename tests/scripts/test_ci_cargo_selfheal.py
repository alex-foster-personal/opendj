"""ci_cargo_selfheal.sh retries a corrupted target/ ONCE, never a real error.

target/ survives across jobs on the persistent self-hosted runners
(scripts/ci_clean_untracked.sh) so cargo can reuse its fingerprints. That
reuse occasionally reads a half-written .rlib/.so left by a job killed
mid-write, and cargo reports the corruption as a compile error (E0786 /
E0463) rather than a torn file -- job 103936733320 (PR #2624, runner
nucbox-wsl-12, Mon 14 Sep 2026) failed exactly this way. The wrapper must
clean and retry on that signature only; a genuine compile error must stay
red on the first failure, with no retry and no `cargo clean`.

Acceptance:
- [if] the build fails with the exact E0786 "found invalid metadata files"
  text [then] the script warns, cleans the given profile, retries once, and
  exits 0 when the retry succeeds.
- [if] the build fails with E0463 naming a crate that IS in Cargo.lock
  [then] the same clean-and-retry happens.
- [if] the build fails with a genuine error (E0425, or an E0463 naming a
  crate NOT in Cargo.lock) [then] the script exits non-zero, `cargo clean` is
  never invoked, and the build command runs exactly once [⛔️ masking a real
  compile error is worse than the flake this heals].
- [if] the build succeeds on the first try [then] `cargo clean` is never
  invoked and the command runs exactly once.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci_cargo_selfheal.sh"

E0786_FIXTURE = (
    "error[E0786]: found invalid metadata files for crate `time_macros` "
    'in "/home/runner/actions-runner-12/_work/music-dj-tools/music-dj-tools/'
    'apps/desktop/src-tauri/target/debug/deps/libtime_macros-abc123.so", '
    "found invalid metadata version found: 6\n"
)
E0463_KNOWN_FIXTURE = "error[E0463]: can't find crate for `proc_macro_error`\n"
E0425_FIXTURE = "error[E0425]: cannot find value `frobnicate` in this scope\n"
E0463_UNKNOWN_FIXTURE = "error[E0463]: can't find crate for `not_a_real_crate`\n"


def _write_fake_cargo(
    bin_dir: Path,
    counter: Path,
    fixture: Path,
    clean_log: Path,
    fail_first: bool = True,
) -> Path:
    """A fake `cargo` binary: `clean ...` logs and exits 0; `selfheal-build`
    fails with `fixture`'s content on its first invocation (unless
    `fail_first` is False) and succeeds (no output) on every later one,
    tracked in `counter`."""
    path = bin_dir / "cargo"
    fail_branch = '[ "$n" -eq 0 ]' if fail_first else "false"
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [ "$1" = "clean" ]; then\n'
        '  echo "$*" >> ' + str(clean_log) + "\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "selfheal-build" ]; then\n'
        '  n=$(cat ' + str(counter) + ' 2>/dev/null || echo 0)\n'
        '  echo $((n + 1)) > ' + str(counter) + "\n"
        f"  if {fail_branch}; then\n"
        "    cat " + str(fixture) + "\n"
        "    exit 101\n"
        "  fi\n"
        '  echo "build ok"\n'
        "  exit 0\n"
        "fi\n"
        'echo "fake cargo: unknown subcommand $*" >&2\n'
        "exit 2\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _run_selfheal(
    tmp_path: Path,
    fixture_text: str,
    profile: str = "dev",
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    manifest_dir = tmp_path / "crate"
    manifest_dir.mkdir()
    manifest = manifest_dir / "Cargo.toml"
    manifest.write_text("[package]\nname = \"fixture\"\n", encoding="utf-8")
    (manifest_dir / "Cargo.lock").write_text(
        'name = "time_macros"\nversion = "0.2.0"\n\n'
        'name = "proc_macro_error"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )

    counter = tmp_path / "counter"
    clean_log = tmp_path / "clean.log"
    fixture = tmp_path / "fixture.txt"
    fixture.write_text(fixture_text, encoding="utf-8")

    _write_fake_cargo(bin_dir, counter, fixture, clean_log)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["RUNNER_NAME"] = "nucbox-wsl-test"

    result = subprocess.run(
        ["bash", str(SCRIPT), str(manifest), profile, "cargo", "selfheal-build"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return result, counter, clean_log


def _call_count(counter: Path) -> int:
    return int(counter.read_text(encoding="utf-8").strip()) if counter.exists() else 0


#-----------------------------------------------------------------------------
# positive: the two corruption signatures self-heal
#-----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fixture_text",
    [E0786_FIXTURE, E0463_KNOWN_FIXTURE],
    ids=["E0786-invalid-metadata", "E0463-crate-in-lockfile"],
)
def test_corrupted_target_signature_heals_and_retries_once(tmp_path, fixture_text) -> None:
    """[if] cargo reports E0786 or a lockfile-known E0463 [then] clean once and retry, [else stop]."""
    result, counter, clean_log = _run_selfheal(tmp_path, fixture_text)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _call_count(counter) == 2, "build must run exactly twice: fail, then retry"
    assert clean_log.exists(), "cargo clean must run on the corruption signature"
    assert "--profile dev" in clean_log.read_text(encoding="utf-8")
    assert "::warning::" in result.stdout


#-----------------------------------------------------------------------------
# negative: a real compile error is never retried or cleaned away
#-----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "fixture_text",
    [E0425_FIXTURE, E0463_UNKNOWN_FIXTURE],
    ids=["E0425-genuine-error", "E0463-crate-absent-from-lockfile"],
)
def test_genuine_compile_error_stays_red_without_retry(tmp_path, fixture_text) -> None:
    """[if] the failure is a genuine compile error [then] no retry and no clean, [else stop]."""
    result, counter, clean_log = _run_selfheal(tmp_path, fixture_text)

    assert result.returncode == 101, result.stdout + result.stderr
    assert _call_count(counter) == 1, "a genuine error must not be retried"
    assert not clean_log.exists(), "cargo clean must never run for a genuine error"


def test_first_try_success_never_cleans_or_retries(tmp_path) -> None:
    """[if] the first build succeeds [then] cargo clean and a retry never run, [else stop]."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    manifest_dir = tmp_path / "crate"
    manifest_dir.mkdir()
    manifest = manifest_dir / "Cargo.toml"
    manifest.write_text("[package]\nname = \"fixture\"\n", encoding="utf-8")
    (manifest_dir / "Cargo.lock").write_text("", encoding="utf-8")

    counter = tmp_path / "counter"
    clean_log = tmp_path / "clean.log"
    fixture = tmp_path / "fixture.txt"
    fixture.write_text(E0786_FIXTURE, encoding="utf-8")

    _write_fake_cargo(bin_dir, counter, fixture, clean_log, fail_first=False)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"

    result = subprocess.run(
        ["bash", str(SCRIPT), str(manifest), "dev", "cargo", "selfheal-build"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _call_count(counter) == 1, "a first-try success must not be retried"
    assert not clean_log.exists()
