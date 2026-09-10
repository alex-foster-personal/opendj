"""An unavailable leak measurement must reach the exit code.

Codex P2 on #705: `report_leaks` printed UNKNOWN and returned 0 when
`/usr/bin/leaks` produced no summary line, so a run whose leak measurement was
blocked by hardened-runtime permissions still ended `[OK] done` with exit 0 and
automation recorded it as a successful profiling run.

Driven against the SHIPPED function, extracted from `scripts/perf/perf_alloc.sh`
by name, and against the REAL `/usr/bin/leaks` - no stub, no fake output. Both
directions come from the tool itself:

  pid 1 (launchd)   privileges refused, no summary  -> the unavailable case
  a process we own  a real summary line             -> the control

macOS only, and skipped elsewhere as UNAVAILABLE rather than passed: `leaks`,
`vmmap` and `footprint` do not exist on Linux, and this script is macOS-only by
construction (see its header). A skip here is a capability report.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import time

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "perf" / "perf_alloc.sh"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not pathlib.Path("/usr/bin/leaks").exists(),
    reason=(
        "UNAVAILABLE: /usr/bin/leaks is macOS-only and perf_alloc.sh is a macOS "
        "script. This is a capability report, not a pass."
    ),
)


def _shipped_report_leaks() -> str:
    """The function as it ships, cut out by name rather than retyped."""
    source = SCRIPT.read_text()
    match = re.search(r"^report_leaks\(\) \{.*?^\}", source, re.MULTILINE | re.DOTALL)
    assert match is not None, "report_leaks() is no longer a top-level function"
    return match.group(0)


def _run(pid: int, out: pathlib.Path) -> subprocess.CompletedProcess[str]:
    program = f"{_shipped_report_leaks()}\nreport_leaks {pid} '{out}'\n"
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_a_leak_measurement_that_could_not_run_is_not_reported_as_success(
    tmp_path: pathlib.Path,
) -> None:
    """If broken: `[OK] done`, exit 0, and a leak count nobody measured."""
    result = _run(1, tmp_path / "launchd.leaks.txt")

    assert "privileges" in result.stdout or "not valid" in result.stdout, (
        "leaks did not refuse pid 1 on this machine, so this arm is not "
        f"exercising the unavailable path at all: {result.stdout!r}"
    )
    assert "UNKNOWN" in result.stdout
    assert result.returncode == 5, (
        f"an unmeasurable leak check returned {result.returncode}, so the run "
        "that contains it can still finish successfully"
    )


def test_a_leak_measurement_that_did_run_still_succeeds(
    tmp_path: pathlib.Path,
) -> None:
    """The control. Without it, "returns nonzero" is satisfied by a function
    that fails on every process, including the ones it measured perfectly."""
    child = subprocess.Popen(["sleep", "60"])
    try:
        time.sleep(0.3)
        assert child.poll() is None, "the subject died before it could be measured"
        result = _run(child.pid, tmp_path / "own.leaks.txt")
    finally:
        child.kill()
        child.wait()

    assert re.search(r"Process \d+: \d+ leaks? for \d+ total leaked bytes", result.stdout), (
        f"leaks produced no summary for a process we own: {result.stdout!r}"
    )
    assert "UNKNOWN" not in result.stdout
    assert result.returncode == 0, (
        f"a leak check that DID measure was reported as unavailable: {result.stderr!r}"
    )


def test_the_shipped_script_accumulates_and_exits_on_an_unavailable_leak_check() -> None:
    """The wiring between the two, read from the shipped source.

    The end-to-end path needs two live Open DJ processes and a hardened-runtime
    refusal on one of them, which cannot be arranged here. What CAN be checked
    is that the accumulator exists, that the loop feeds it, and that the tail
    turns it into a nonzero exit - so a later edit cannot quietly restore the
    "print UNKNOWN, exit 0" shape the finding was about.
    """
    source = SCRIPT.read_text()
    assert "LEAKS_UNAVAILABLE=$((LEAKS_UNAVAILABLE + 1))" in source
    assert re.search(
        r'if \[\[ "\$LEAKS_UNAVAILABLE" -gt 0 \]\]; then(?:.|\n)*?\n    exit 5\n',
        source,
    ), "the accumulator no longer reaches a nonzero exit"
    assert os.access(SCRIPT, os.X_OK), "the script must stay executable"
