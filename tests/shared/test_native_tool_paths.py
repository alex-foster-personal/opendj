"""Native macOS tools are called by absolute path, never through PATH.

``sysctl`` lives in ``/usr/sbin``. A process started by a launchd agent, a
packaged app's sidecar, or anything else that sets its own PATH need not have
``/usr/sbin`` on it, and a bare ``["sysctl", ...]`` argv then raises
``FileNotFoundError`` before the command ever runs.

Found live Wed 16 Sep 2026 on the launchd-supervised review preview:
``GET /api/v1/performance/telemetry/pressure`` answered
``available: false, native machine sampler failed: [Errno 2] No such file or
directory: 'sysctl'``. The agent's plist PATH is
``~/.local/bin:/opt/homebrew/opt/node@22/bin:/opt/homebrew/bin:/usr/local/bin:
/usr/bin:/bin`` -- no ``/usr/sbin``. So every PERFMODE-04 pressure shed and the
Q29 closed-loop cap stepper were blind on the preview that gets reviewed most,
while the same engine started from an interactive shell sampled perfectly.
``_sysctl`` catches ``CalledProcessError`` and ``TimeoutExpired`` but not
``FileNotFoundError``, so one missing binary took down the whole sample rather
than three fields of it.

Regression lines:
- if the sampler resolves sysctl through PATH then a launchd-started engine
  reports pressure unavailable and no shed can fire
- if live-stems capability resolves sysctl through PATH then the capability
  probe raises under launchd instead of naming the machine
- if any module reintroduces a bare "sysctl" argv then the first two can come
  back without either test above running on that path
"""

from __future__ import annotations

import platform
import re
from pathlib import Path

import pytest

from apps.diagnostics.probe_native_metrics import machine_metrics
from apps.native_tools import SYSCTL, sysctl_n, try_sysctl_n
from apps.stems.live_capability import detect_installed_machine_name

# The preview engine plist's PATH, Wed 16 Sep 2026, with its home directory
# made portable. The absence of /usr/sbin is the whole point.
LAUNCHD_AGENT_PATH = (
    f"{Path.home()}/.local/bin:/opt/homebrew/opt/node@22/bin:"
    "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_darwin_only = pytest.mark.skipif(
    platform.system() != "Darwin", reason="sysctl and vm_stat are macOS tools"
)


@_darwin_only
def test_machine_sampler_reads_pressure_under_a_launchd_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", LAUNCHD_AGENT_PATH)
    metrics = machine_metrics()
    assert metrics.get("kernel_memory_pressure_level") in (1, 2, 4), (
        "if the sampler resolves sysctl through PATH then a launchd-started "
        f"engine cannot read kernel memory pressure - broken: {metrics!r}"
    )
    assert metrics.get("physical_memory_mb", 0) > 0, (
        "hw.memsize must be readable without /usr/sbin on PATH - broken"
    )
    assert "swap_used_mb" in metrics, (
        "vm.swapusage must be readable without /usr/sbin on PATH - broken"
    )


@_darwin_only
def test_live_stems_names_the_machine_under_a_launchd_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", LAUNCHD_AGENT_PATH)
    name = detect_installed_machine_name()
    assert name.strip(), (
        "if live-stems capability resolves sysctl through PATH then it cannot "
        "name the machine under launchd - broken"
    )


@_darwin_only
def test_sysctl_n_reads_hw_memsize() -> None:
    result = sysctl_n("hw.memsize")
    assert result.returncode == 0
    assert result.stdout.strip().isdigit()
    assert int(result.stdout.strip()) > 0


@_darwin_only
def test_try_sysctl_n_reads_hw_memsize() -> None:
    value = try_sysctl_n("hw.memsize")
    assert value is not None
    assert value.isdigit()
    assert int(value) > 0


@_darwin_only
def test_try_sysctl_n_returns_none_for_unknown_oid() -> None:
    assert try_sysctl_n("kern.open_dj_nonexistent_oid") is None


def test_sysctl_constant_is_absolute_path() -> None:
    assert SYSCTL == "/usr/sbin/sysctl"


def test_no_module_calls_sysctl_by_bare_name() -> None:
    """Platform-independent guard, so Linux CI still catches a regression."""
    bare = re.compile(r"""\[\s*["']sysctl["']""")
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{lineno}"
        for path in sorted((REPO_ROOT / "apps").rglob("*.py"))
        # Third-party installs CI keeps between jobs are not our modules: node-gyp's
        # ninja.py tripped this on agentbox-9 (Fri 2 Oct 2026).
        if "node_modules" not in path.relative_to(REPO_ROOT).parts
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if bare.search(line)
    ]
    assert offenders == [], (
        "if any module reintroduces a bare 'sysctl' argv then a launchd or "
        f"packaged process loses it again - broken at: {offenders}"
    )
