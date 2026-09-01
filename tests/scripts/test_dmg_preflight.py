"""OPS-11: the dmg preflight reports EVERY unmet precondition in one pass.

The bug this pins: `just dmg` used to validate its preconditions in sequence
(dirty tree, then signing identity, then unbuilt SPA, then the pnpm toolchain
pin), so an operator with four problems paid four failed runs to learn about
them. Independent checks must not be reported one per attempt.

Every case here runs the REAL script against a throwaway git repository
(MDT_REPO_ROOT) with a PATH shim for pnpm, so nothing depends on how the
developer's own machine happens to be set up and nothing touches this repo.

Regression lines:
  - if the tree is dirty AND pnpm is unpinned and only one of them is
    reported then broken
  - if a passing check is not reported alongside failing ones then the report
    cannot distinguish pass from fail and is broken
  - if a check that cannot MEASURE its subject reports a pass then broken
  - if the pnpm version is read from the repo root rather than the frontend
    directory then a corepack-pinned machine is called unpinned and broken
  - if any unmet precondition still exits 0 then broken
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("OPS-11")

REPO = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO / "scripts" / "dmg_preflight.sh"
PINNED_PNPM = "11.9.0"
SHIMMED_PNPM = "10.0.0"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _fixture_repo(tmp_path: Path, *, dirty: bool, pin: str | None = PINNED_PNPM) -> Path:
    """A minimal checkout: committed frontend package.json, optionally dirty."""
    repo = tmp_path / "repo"
    frontend = repo / "apps" / "webui" / "frontend"
    frontend.mkdir(parents=True)
    manager = f'\n  "packageManager": "pnpm@{pin}",' if pin else ""
    (frontend / "package.json").write_text(
        '{\n  "name": "fixture",' + manager + '\n  "private": true\n}\n',
        encoding="utf-8",
    )
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fixture")
    if dirty:
        (repo / "uncommitted.txt").write_text("edited\n", encoding="utf-8")
    return repo


def _pnpm_shim(tmp_path: Path, version: str) -> Path:
    """A pnpm that answers --version and nothing else, first on PATH."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir(exist_ok=True)
    pnpm = shim_dir / "pnpm"
    pnpm.write_text(f'#!/bin/bash\necho "{version}"\n', encoding="utf-8")
    pnpm.chmod(0o755)
    return shim_dir


def _run_preflight(repo: Path, shim_dir: Path) -> subprocess.CompletedProcess[str]:
    # MDT_SHIP_UNSIGNED=1 is a DELIBERATE pass: it gives the report at least
    # one [OK] line, which is what proves a failing report is reporting
    # failures rather than simply failing everything it looks at.
    env = dict(os.environ)
    env.pop("MDT_MACOS_SIGNING_IDENTITY", None)
    env.pop("MDT_MACOS_NOTARY_KEYCHAIN_PROFILE", None)
    env["MDT_SHIP_UNSIGNED"] = "1"
    env["MDT_REPO_ROOT"] = str(repo)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        [str(PREFLIGHT)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )


def test_a_dirty_tree_and_an_unpinned_pnpm_are_reported_together(tmp_path: Path) -> None:
    """The headline regression: two independent failures, ONE run."""
    repo = _fixture_repo(tmp_path, dirty=True)
    result = _run_preflight(repo, _pnpm_shim(tmp_path, SHIMMED_PNPM))
    out = result.stdout + result.stderr

    assert result.returncode == 1, out
    assert "working tree is dirty" in out, out
    assert f"pnpm resolves to {SHIMMED_PNPM}" in out, out
    assert f"pins pnpm@{PINNED_PNPM}" in out, out
    # Both must survive into the single summary line an agent relays, so the
    # operator does not have to scroll the report to learn the second one.
    report = [line for line in out.splitlines() if line.startswith("REPORT TO USER:")]
    assert len(report) == 1, out
    assert "working tree" in report[0], report[0]
    assert "pnpm toolchain pin" in report[0], report[0]


def test_the_report_still_passes_the_checks_that_pass(tmp_path: Path) -> None:
    """Control: a failing report must still be able to say [OK].

    A report that failed everything would satisfy the test above without
    measuring anything, so the signing configuration is deliberately set to a
    valid state and has to come back green in the same run.
    """
    repo = _fixture_repo(tmp_path, dirty=True)
    result = _run_preflight(repo, _pnpm_shim(tmp_path, SHIMMED_PNPM))
    out = result.stdout + result.stderr
    assert "[OK]      MDT_SHIP_UNSIGNED=1" in out, out


def test_a_clean_tree_and_a_matching_pnpm_are_not_reported_as_failures(
    tmp_path: Path,
) -> None:
    """The other half of the control: neither check fires when it should not."""
    repo = _fixture_repo(tmp_path, dirty=False)
    result = _run_preflight(repo, _pnpm_shim(tmp_path, PINNED_PNPM))
    out = result.stdout + result.stderr
    assert "working tree is clean" in out, out
    assert f"pnpm {PINNED_PNPM} in the frontend directory matches the pin" in out, out
    # The run still fails: this fixture has no built SPA. That is the point of
    # asserting on the named checks rather than on the exit code alone.
    assert "working tree" not in out.split("REPORT TO USER:")[-1], out


def test_a_check_that_cannot_measure_reports_a_failure_never_a_pass(
    tmp_path: Path,
) -> None:
    """An absent package.json means UNKNOWN, which is a failure, not a pass."""
    repo = _fixture_repo(tmp_path, dirty=False, pin=None)
    (repo / "apps" / "webui" / "frontend" / "package.json").unlink()
    result = _run_preflight(repo, _pnpm_shim(tmp_path, PINNED_PNPM))
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "so the pinned pnpm version cannot be read" in out, out
    assert "pnpm toolchain pin" in out.split("REPORT TO USER:")[-1], out


def test_the_pnpm_version_is_measured_in_the_frontend_directory(tmp_path: Path) -> None:
    """corepack resolves packageManager from the CURRENT directory.

    Asking the repo root reports whatever pnpm is on PATH, which calls a
    correctly pinned machine unpinned. The shim answers with the pin ONLY when
    it is invoked from the frontend directory, so a preflight that measures
    from the root fails this test.
    """
    repo = _fixture_repo(tmp_path, dirty=False)
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir(exist_ok=True)
    frontend = repo / "apps" / "webui" / "frontend"
    pnpm = shim_dir / "pnpm"
    pnpm.write_text(
        "#!/bin/bash\n"
        f'if [ "$PWD" = "{frontend}" ]; then echo "{PINNED_PNPM}"; '
        f'else echo "{SHIMMED_PNPM}"; fi\n',
        encoding="utf-8",
    )
    pnpm.chmod(0o755)
    result = _run_preflight(repo, shim_dir)
    out = result.stdout + result.stderr
    assert f"pnpm {PINNED_PNPM} in the frontend directory matches the pin" in out, out
