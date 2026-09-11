"""CLI: `python -m apps.parity score --payload` prints a real report."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.parity.payloads import MEASURED_AT

pytestmark = pytest.mark.requirement("PARITY-01")


def test_score_cli_prints_denominators_and_the_measurement_date() -> None:
    """[if] the CLI scores a fixture payload [then] stdout names each scored
    lane's denominator, the measurement date, and the scorer version, and
    does not claim parity.
    """
    path = Path(__file__).parent / "fixtures" / "round0.json"
    done = subprocess.run(
        [sys.executable, "-m", "apps.parity", "score", "--payload", str(path)],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert MEASURED_AT in out
    assert "scorer" in out.lower()
    assert "bpm" in out.lower()
    assert "key" in out.lower()
    assert "at parity" not in out.lower()
    assert "delegated" in out.lower()
    assert "not_scored_this_round" in out or "not scored this round" in out.lower()
