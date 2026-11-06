"""OPS-11 / INSTALL-20: dmg preflight Icon Composer actool and fallback checks.

Split from test_dmg_preflight.py so the Icon Composer regression stays
macOS-scoped without bloating the core one-pass suite.

Regression lines:
  - if actool is absent and no valid committed fallback exists then the
    preflight fails in seconds naming actool and the host before any compile
  - if the committed fallback matches the source digest then a no-actool host
    reports reuse readiness and can proceed
  - if the Icon Composer check fails then later sections and the summary still
    appear

[if] actool is absent with no valid fallback [then] preflight fails before compile, [else stop].
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.icon_composer_asset import PayloadBuildError, find_actool
from tests.scripts.icon_composer_harness import path_without_actool_resolver

REPO = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO / "scripts" / "dmg_preflight.sh"
ICON_SOURCE = REPO / "apps/desktop/shared/icons/AppIcon.icon"
COMMITTED_CAR = ICON_SOURCE / "Assets.car"
COMMITTED_SIDECAR = ICON_SOURCE / "Assets.car.source.sha256"


def _is_dmg_preflight_host() -> bool:
    """True when this machine is the DMG build host or opted in via env."""
    if os.environ.get("OPEN_DJ_DMG_HOST", "").strip():
        return True
    short = socket.gethostname().lower()
    nodename = os.uname().nodename.lower()
    haystack = f"{short} {nodename}"
    return any(
        hint in haystack for hint in ("mac-studio", "mac studio", "silver")
    )


darwin_only = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="UNAVAILABLE: Icon Composer preflight checks need macOS",
)
xcrun_on_path = pytest.mark.skipif(
    shutil.which("xcrun") is None,
    reason=(
        "UNAVAILABLE: xcrun is not on PATH, so Icon Composer preflight "
        "isolation cannot be measured"
    ),
)
dmg_preflight_host = pytest.mark.skipif(
    not _is_dmg_preflight_host(),
    reason=(
        "UNAVAILABLE: Icon Composer preflight host checks run on the DMG host; "
        "set OPEN_DJ_DMG_HOST to measure elsewhere"
    ),
)

pytestmark = [
    pytest.mark.requirement("OPS-11"),
    pytest.mark.requirement("INSTALL-20"),
    darwin_only,
    xcrun_on_path,
    dmg_preflight_host,
]


def _disposable_icon_tree(tmp_path: Path, *, include_fallback: bool) -> Path:
    root = tmp_path / ("with-fallback" if include_fallback else "no-fallback")
    icon_source = root / "apps/desktop/shared/icons/AppIcon.icon"
    shutil.copytree(ICON_SOURCE, icon_source)
    if not include_fallback:
        (icon_source / "Assets.car").unlink(missing_ok=True)
        (icon_source / "Assets.car.source.sha256").unlink(missing_ok=True)
    frontend = root / "apps/webui/frontend"
    frontend.mkdir(parents=True)
    shutil.copy2(
        REPO / "apps/webui/frontend/package.json",
        frontend / "package.json",
    )
    git = ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-qm", "hydrate"], check=True, capture_output=True)
    return root


def _run_preflight(
    *,
    path: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
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


def test_native_actool_resolves_without_the_shim() -> None:
    """Positive control: find_actool() succeeds on an unshimmed PATH when actool
    exists, so the other tests' shimmed-PATH failures are known to come from
    the shim and not from a host that could never find actool anyway."""
    try:
        found = find_actool()
    except PayloadBuildError as exc:
        pytest.skip(
            f"UNAVAILABLE: xcrun --find actool fails natively on this host, so the positive control cannot run: {exc}"
        )
    assert found.is_file(), found


def test_no_actool_without_fallback_is_refused_before_compile(tmp_path: Path) -> None:
    """Absent actool with no valid committed fallback fails in the preflight."""
    path = path_without_actool_resolver()
    if path is None:
        pytest.skip("UNAVAILABLE: xcrun is not on PATH, so actool isolation cannot be measured")
    root = _disposable_icon_tree(tmp_path, include_fallback=False)
    host_sock = socket.gethostname()
    host_uname = os.uname().nodename
    result = _run_preflight(path=path, extra_env={"MDT_REPO_ROOT": str(root)})
    out = result.stdout + result.stderr

    assert result.returncode == 1, out
    assert "=== J. Icon Composer compiled asset" in out, out
    assert "actool" in out.lower(), out
    assert host_sock in out or host_uname in out, out
    assert "Icon Composer asset" in _report_line(out), out
    assert "=== summary ===" in out, out


def test_no_actool_with_current_fallback_reports_reuse_readiness(tmp_path: Path) -> None:
    """A verified committed fallback lets a no-actool host proceed."""
    if not COMMITTED_CAR.is_file() or not COMMITTED_SIDECAR.is_file():
        pytest.skip("UNAVAILABLE: committed Assets.car fallback is not present in this checkout")
    path = path_without_actool_resolver()
    if path is None:
        pytest.skip("UNAVAILABLE: xcrun is not on PATH, so actool isolation cannot be measured")
    root = _disposable_icon_tree(tmp_path, include_fallback=True)
    result = _run_preflight(path=path, extra_env={"MDT_REPO_ROOT": str(root)})
    out = result.stdout + result.stderr

    assert "[OK]      Icon Composer: no actool on" in out, out
    assert "reuse-committed path=" in out, out
    assert "Icon Composer asset" not in _report_line(out), out


def test_icon_composer_failure_does_not_truncate_the_report(tmp_path: Path) -> None:
    """A missing icon capability must not end the report before later sections."""
    path = path_without_actool_resolver()
    if path is None:
        pytest.skip("UNAVAILABLE: xcrun is not on PATH, so actool isolation cannot be measured")
    root = _disposable_icon_tree(tmp_path, include_fallback=False)
    result = _run_preflight(path=path, extra_env={"MDT_REPO_ROOT": str(root)})
    out = result.stdout + result.stderr

    assert "=== I. Google OAuth client" in out, out
    assert "=== summary ===" in out, out
    assert result.returncode == 1, out
