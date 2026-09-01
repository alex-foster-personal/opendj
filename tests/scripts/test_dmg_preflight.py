"""OPS-11: the dmg preflight reports EVERY unmet precondition in one pass.

The bug this pins: `just dmg` used to validate its preconditions in sequence
(dirty tree, then signing identity, then unbuilt SPA, then the pnpm toolchain
pin), so an operator with four problems paid four failed runs to learn about
them. Independent checks must not be reported one per attempt.

REAL PATH, AND THIS CHECKOUT IS NEVER WRITTEN TO. These run the production
script with the real git, the real corepack and the real signing environment.
Nothing is stubbed, shimmed or fabricated (AGENTS.md, "No mocks and locked
real fixtures").

Anything that needs a tree in a particular state gets a DISPOSABLE HYDRATED
COPY, pointed at through the script's own `MDT_REPO_ROOT`, whose documented
purpose is exactly this. The copy carries the REAL `package.json` bytes, so
the pin under test is the shipped pin rather than an invented one. The tests
that do not need a specific state read THIS checkout without writing to it.
That distinction is the point: `just test` runs pytest under xdist, so a test
that edited a tracked file would be visible to every other worker mid-run,
and a worker killed between its edit and its restore would leave the edit
behind permanently.

Two more levers produce real failures out of nothing but the environment:

  * an EMPTY `COREPACK_HOME` plus `COREPACK_ENABLE_NETWORK=0`, so the real
    corepack genuinely cannot resolve the real pin. That is how a machine
    which has never fetched the pinned version behaves, and it is the exact
    scenario that used to end the script mid-report;
  * a PATH with the directories providing a tool removed, which is a real
    environment in which that dependency is genuinely unavailable.

Where a capability is not available on the running machine, the test SKIPS
naming why, rather than manufacturing a pass.

NOT COVERED HERE, deliberately: the same probe failure for `uv`, which would
need a deliberately broken binary. Verified by hand on the Air, Tue 1 Sep
2026: before the fix a uv that exits nonzero printed `[OK] uv present ()`;
after it, the run reports an unmet precondition naming uv. Both probes go
through the one helper that the corepack test exercises for real.

Regression lines:
  - if the tree is dirty AND pnpm cannot satisfy the pin and only one of them
    is reported then broken
  - if a version probe that FAILS ends the report instead of appearing in it
    then broken
  - if a passing check is not reported alongside failing ones then the report
    cannot distinguish pass from fail and is broken
  - if the tree check disagrees with what git reports then broken
  - if the pnpm version is read from the repo root rather than the frontend
    directory then a corepack-pinned machine is called unpinned and broken
  - if any unmet precondition still exits 0 then broken
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("OPS-11")

REPO = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO / "scripts" / "dmg_preflight.sh"
FRONTEND = REPO / "apps" / "webui" / "frontend"
PACKAGE_JSON = FRONTEND / "package.json"


def _path_without(tool: str) -> str:
    """The real PATH minus every directory that provides ``tool``."""
    kept = [
        part
        for part in os.environ.get("PATH", "").split(os.pathsep)
        if part and not (Path(part) / tool).exists()
    ]
    return os.pathsep.join(kept)


def _disposable_tree(tmp_path: Path, *, dirty: bool) -> Path:
    """A throwaway git checkout carrying the REAL pinned package.json.

    Hydrated from the shipped file rather than written from a template, so
    the pin the preflight reads here is the pin the build would really use.
    Nothing in the shared checkout is touched.
    """
    root = tmp_path / ("dirty" if dirty else "clean")
    frontend = root / "apps" / "webui" / "frontend"
    frontend.mkdir(parents=True)
    shutil.copy2(PACKAGE_JSON, frontend / "package.json")
    git = ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-qm", "hydrate"], check=True, capture_output=True)
    if dirty:
        (root / "uncommitted.txt").write_text("real uncommitted work\n", encoding="utf-8")
    return root


def _offline_corepack(tmp_path: Path) -> dict[str, str]:
    """An environment where the real corepack cannot resolve the real pin.

    An empty cache plus no network. The repository is not modified in any
    way: the pin stays exactly as committed, and it is the MACHINE that
    cannot satisfy it, which is the real-world failure being reproduced.

    The isolation is VERIFIED here rather than assumed, because these three
    variables only bite when `pnpm` is a Corepack SHIM. A standalone pnpm
    ignores all of them, answers `--version` happily, and the pin failure
    every caller below asserts on never happens - so those tests would go red
    on a machine whose production preflight is entirely correct. That is a
    false alarm, not a finding, and a suite that cries wolf on a healthy
    machine is worse than one test fewer. The probe is therefore run under
    this exact environment and the caller is skipped UNAVAILABLE, naming what
    was measured, when the isolation does not bite.
    """
    home = tmp_path / "corepack-home"
    home.mkdir()
    env = {
        "COREPACK_HOME": str(home),
        "COREPACK_ENABLE_NETWORK": "0",
        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
    }
    if shutil.which("pnpm") is None:
        pytest.skip("UNAVAILABLE: pnpm is not installed, so its pin cannot fail")
    probe = subprocess.run(
        ["pnpm", "--version"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**os.environ, **env},
        cwd=REPO,
    )
    if probe.returncode == 0:
        pytest.skip(
            "UNAVAILABLE: pnpm answered --version under an empty COREPACK_HOME "
            "with the network disabled, so it is a standalone install rather "
            "than a Corepack shim and its pin cannot be made to fail on this "
            f"machine (it reported {probe.stdout.strip()!r})"
        )
    return env


def _run_preflight(
    path: str | None = None, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    # MDT_SHIP_UNSIGNED=1 is a real, valid signing configuration and a
    # DELIBERATE pass: it gives the report at least one [OK] line, which is
    # what proves a failing report is reporting failures rather than simply
    # failing everything it looks at.
    env = dict(os.environ)
    env.pop("MDT_MACOS_SIGNING_IDENTITY", None)
    env.pop("MDT_MACOS_NOTARY_KEYCHAIN_PROFILE", None)
    env["MDT_SHIP_UNSIGNED"] = "1"
    if path is not None:
        env["PATH"] = path
    env.update(extra_env or {})
    return subprocess.run(
        [str(PREFLIGHT)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=env,
        cwd=REPO,
    )


def _report_line(out: str) -> str:
    lines = [line for line in out.splitlines() if line.startswith("REPORT TO USER:")]
    assert len(lines) == 1, f"expected exactly one report line:\n{out}"
    return lines[0]


def test_a_dirty_tree_and_an_unsatisfiable_pnpm_pin_are_reported_together(
    tmp_path: Path,
) -> None:
    """The headline regression: two independent failures, ONE run."""
    root = _disposable_tree(tmp_path, dirty=True)
    result = _run_preflight(
        extra_env={"MDT_REPO_ROOT": str(root), **_offline_corepack(tmp_path)}
    )
    out = result.stdout + result.stderr

    assert result.returncode == 1, out
    assert "working tree is dirty" in out, out
    # Both must survive into the single line an agent relays, so the operator
    # does not have to scroll the report to learn the second one.
    report = _report_line(out)
    assert "working tree" in report, report
    assert "pnpm toolchain pin" in report, report


def test_a_failing_version_probe_appears_in_the_report_instead_of_ending_it(
    tmp_path: Path,
) -> None:
    """A probe that cannot answer must not take the report down with it.

    Under `set -e` plus `pipefail`, `x="$(broken --version | head -1)"` ends
    the script, so the branch written to report the failure never runs and
    the operator gets a truncated report with no summary.
    """
    if shutil.which("pnpm") is None:
        pytest.skip("UNAVAILABLE: pnpm is not installed, so its probe cannot fail")
    root = _disposable_tree(tmp_path, dirty=False)
    result = _run_preflight(
        extra_env={"MDT_REPO_ROOT": str(root), **_offline_corepack(tmp_path)}
    )
    out = result.stdout + result.stderr

    assert "so the toolchain cannot be checked against" in out, out
    # THE REGRESSION: the section AFTER the failing probe, and the summary,
    # must still be there. Their absence is what a script-ending probe looks
    # like, and it exits with the probe's own code rather than 1.
    assert "=== G. built SPA ===" in out, out
    assert "pnpm toolchain pin" in _report_line(out), out
    assert result.returncode == 1, out


def test_the_report_still_passes_the_checks_that_pass(tmp_path: Path) -> None:
    """Control: a failing report must still be able to say [OK].

    A report that failed everything would satisfy both tests above without
    measuring anything. This run is against a CLEAN disposable tree with a
    valid signing configuration, so two checks have to come back green in
    the same run that reports failures.
    """
    root = _disposable_tree(tmp_path, dirty=False)
    result = _run_preflight(
        extra_env={"MDT_REPO_ROOT": str(root), **_offline_corepack(tmp_path)}
    )
    out = result.stdout + result.stderr
    assert "[OK]      MDT_SHIP_UNSIGNED=1" in out, out
    assert "[OK]      working tree is clean" in out, out
    assert "[ERROR]" in out, out
    assert "working tree" not in _report_line(out), out


def test_the_tree_check_reports_what_git_reports() -> None:
    """An invariant, not a forced state: the check agrees with git.

    Asserting this WITHOUT writing to the shared checkout is the point. Both
    branches carry a real assertion, whichever state the tree is in when the
    suite runs.
    """
    dirty = subprocess.run(
        ["git", "-C", str(REPO), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    out = _run_preflight().stdout
    if dirty:
        assert "working tree is dirty" in out, out
        assert "working tree" in _report_line(out), out
    else:
        assert "working tree is clean" in out, out
        assert "working tree" not in _report_line(out), out


def test_a_dependency_that_is_absent_is_reported_as_a_failure() -> None:
    """A check that cannot measure its subject reports a failure, never a pass."""
    if shutil.which("uv") is None:
        pytest.skip("UNAVAILABLE: uv is not installed, so it cannot be removed")
    result = _run_preflight(_path_without("uv"))
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "uv not found on PATH" in out, out
    # The SPA freshness check runs THROUGH uv, so it must report that it could
    # not be measured rather than passing or staying silent.
    assert "cannot run the SPA freshness check" in out, out
    assert "uv" in _report_line(out), out


def test_the_pnpm_version_is_measured_in_the_frontend_directory() -> None:
    """corepack resolves packageManager from the CURRENT directory.

    Asking the repo root reports whatever pnpm is on PATH, which calls a
    correctly pinned machine unpinned. Where the running machine really shows
    that divergence, the preflight must report the frontend value.
    """
    if shutil.which("pnpm") is None:
        pytest.skip("UNAVAILABLE: pnpm is not installed")

    def version_in(cwd: Path) -> str:
        proc = subprocess.run(
            ["pnpm", "--version"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return proc.stdout.strip()

    at_root, at_frontend = version_in(REPO), version_in(FRONTEND)
    if not at_frontend:
        pytest.skip("UNAVAILABLE: pnpm did not answer --version in the frontend directory")
    if at_root == at_frontend:
        pytest.skip(
            "UNAVAILABLE: pnpm answers "
            f"{at_frontend} in both directories on this machine, so there is no "
            "divergence to distinguish the two measurement points"
        )
    out = _run_preflight().stdout
    assert f"pnpm {at_frontend} in the frontend directory" in out, out
    assert at_root not in out, out
