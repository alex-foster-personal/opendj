"""Runner outcome classification (AGENT-16).

The point of these tests is the three-way split: measured-pass, measured-fail,
and UNKNOWN. A suite that only checked "did it fail" would pass for a runner
that reported every unreachable binary as a clean zero, which is exactly the
defect `.claude/rules/verification.md` names.
"""

from __future__ import annotations

import pytest

from apps.fleet_mcp.runner import UNKNOWN, run


@pytest.mark.requirement("AGENT-16")
def test_missing_binary_is_unknown_not_failure():
    """[if] the command is not on PATH [then] the outcome is UNKNOWN, not a verdict, [else stop]"""
    completed = run(["definitely-not-a-real-binary-9f3a"], timeout=5)
    assert not completed.measured
    assert completed.returncode is None
    assert "not on PATH" in (completed.unknown_reason or "")
    assert completed.unknown_document()["status"] == UNKNOWN


@pytest.mark.requirement("AGENT-16")
def test_timeout_is_unknown():
    """[if] the command does not finish [then] the outcome is UNKNOWN, [else stop]"""
    completed = run(["sleep", "5"], timeout=0.2)
    assert not completed.measured
    assert "did not finish" in (completed.unknown_reason or "")


@pytest.mark.requirement("AGENT-16")
def test_nonzero_exit_is_measured_with_stderr_kept():
    """[if] the command runs and fails [then] it is MEASURED and stderr survives, [else stop]"""
    completed = run(
        ["python", "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        timeout=10,
    )
    assert completed.measured
    assert not completed.ok
    assert completed.returncode == 3
    assert completed.stderr == "boom"


@pytest.mark.requirement("AGENT-16")
def test_silent_success_is_measured_not_unknown():
    """Negative control for the zero-is-both-a-value-and-an-error-signature case.

    [if] a command exits 0 printing nothing [then] it is a MEASUREMENT of empty,, [else stop]
    and must not be reported the same way as a command that could not run.
    """
    completed = run(["python", "-c", "pass"], timeout=10)
    assert completed.measured
    assert completed.ok
    assert completed.stdout == ""
    assert completed.unknown_reason is None


@pytest.mark.requirement("AGENT-16")
def test_empty_argv_is_a_programming_error():
    """[if] argv is empty [then] the runner raises rather than shelling out, [else stop]"""
    with pytest.raises(ValueError, match="must not be empty"):
        run([], timeout=1)
