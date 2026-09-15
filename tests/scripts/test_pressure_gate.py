"""Hermetic tests for ops/fleet/pressure.sh spawn gate (issue #3002).

Fixture files and PRESSURE_* env seams only - no mocked gate logic.

Regression lines:
  - if comfortable MemAvailable plus stall PSI from Tue 15 Sep 2026 still reports ok
    then broken (must be PRESSURE high with mem_psi in reason=)
  - if mem_low arm does not fire when MemAvailable < 6GB then broken
  - if load arm does not fire when load_per_core > 1.5 then broken
  - if io_psi arm does not fire when io full avg60 exceeds threshold then broken
  - if all calm inputs report high then broken
  - if a missing PSI seam file is treated as zero instead of failing loud then broken
  - if ok path omits mem_psi_full_avg60 or io_psi_full_avg60 then broken
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    platform.system() != "Linux",
    reason="ops/fleet/pressure.sh is Linux-only (/proc, PSI)",
)

REPO = Path(__file__).resolve().parents[2]
PRESSURE = REPO / "ops" / "fleet" / "pressure.sh"
FIXTURE = REPO / "tests" / "fixtures" / "pressure-gate"


def _run_pressure(
    *,
    meminfo: str = "meminfo-comfortable",
    loadavg: str = "loadavg-comfortable",
    memory_psi: str = "pressure-memory-calm",
    io_psi: str = "pressure-io-calm",
    cores: str = "32",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    jobs_dir = FIXTURE
    env = {
        **os.environ,
        "PRESSURE_MEMINFO_FILE": str(FIXTURE / meminfo),
        "PRESSURE_LOADAVG_FILE": str(FIXTURE / loadavg),
        "PRESSURE_MEMORY_PSI_FILE": str(FIXTURE / memory_psi),
        "PRESSURE_IO_PSI_FILE": str(FIXTURE / io_psi),
        "PRESSURE_CORES": cores,
        "PRESSURE_JOBS_DIR": str(jobs_dir),
        "PRESSURE_PS_CMD": "true",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(PRESSURE)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_issue_3002_reproduction_reports_high_on_psi_not_mem() -> None:
    """[if] Tue 15 Sep reproduction fixtures [then] high on PSI, not mem."""
    result = _run_pressure(
        meminfo="meminfo-comfortable",
        loadavg="loadavg-comfortable",
        memory_psi="pressure-memory-stall",
        io_psi="pressure-io-stall",
    )
    line = result.stdout.strip()
    assert result.returncode == 0, result.stderr
    assert line.startswith("PRESSURE high")
    assert "reason=" in line
    assert "mem_psi" in line
    assert "free_slots=0" in line
    assert "mem_avail_gb=34.6" in line


def test_mem_low_arm() -> None:
    """[if] exhausted meminfo and calm PSI/load [then] high reason=mem_low."""
    result = _run_pressure(meminfo="meminfo-exhausted")
    line = result.stdout.strip()
    assert result.returncode == 0, result.stderr
    assert line.startswith("PRESSURE high")
    assert "reason=mem_low" in line


def test_load_arm() -> None:
    """[if] hot loadavg and comfortable mem/PSI [then] high reason=load."""
    result = _run_pressure(loadavg="loadavg-hot", cores="32")
    line = result.stdout.strip()
    assert result.returncode == 0, result.stderr
    assert line.startswith("PRESSURE high")
    assert "reason=load" in line


def test_io_psi_arm() -> None:
    """[if] io stall PSI only [then] high reason=io_psi."""
    result = _run_pressure(io_psi="pressure-io-stall")
    line = result.stdout.strip()
    assert result.returncode == 0, result.stderr
    assert line.startswith("PRESSURE high")
    assert "reason=io_psi" in line


def test_all_clear_reports_ok() -> None:
    """[if] all calm fixtures [then] PRESSURE ok without reason= or top=."""
    result = _run_pressure()
    line = result.stdout.strip()
    assert result.returncode == 0, result.stderr
    assert line.startswith("PRESSURE ok")
    assert "reason=" not in line
    assert " top=" not in line


def test_missing_psi_file_fails_loud() -> None:
    """[if] PSI seam points at absent file [then] non-zero exit naming path."""
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "no-such-psi"
        result = _run_pressure(
            extra_env={"PRESSURE_MEMORY_PSI_FILE": str(missing)},
        )
    assert result.returncode == 2
    assert "no-such-psi" in result.stderr
    assert "unreadable PSI file" in result.stderr


def test_ok_path_includes_psi_fields() -> None:
    """[if] calm inputs [then] stdout includes PSI readout fields."""
    result = _run_pressure()
    line = result.stdout.strip()
    assert result.returncode == 0, result.stderr
    assert "mem_psi_full_avg60=2.00" in line
    assert "io_psi_full_avg60=5.00" in line


@pytest.fixture(scope="module", autouse=True)
def _require_pressure_script() -> None:
    assert PRESSURE.is_file(), f"missing {PRESSURE}"
    assert shutil.which("bash") is not None
