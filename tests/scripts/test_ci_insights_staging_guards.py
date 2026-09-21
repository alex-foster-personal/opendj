"""Guards on the CI Insights staging step's failure behavior.

Extracted from `test_ci_mergify_insights.py` when that module approached the
quality gate's per-file line limit, following the same split as
`autoreposync_uv.py`. The helpers stay in the original module so there is one
definition of how the workflow is loaded.
"""

from tests.scripts.test_ci_mergify_insights import _stage_step


def test_staging_fails_when_a_required_copy_or_write_fails() -> None:
    """A step that cannot fail cannot suppress the upload (Sol P1, round 4).

    The staging script has no errexit, so every command required for the
    staging directory to be COMPLETE must refuse on its own. Otherwise a
    failed copy or write exits 0, continue-on-error records outcome=success,
    and the guard downstream reads a WRITE FAILURE as an expected ABSENCE.
    This is what lets test_a_staging_failure_suppresses_the_upload bite: that
    test pins the upload's condition, this one pins that it can ever be false.
    """
    run = _stage_step()["run"]
    assert "set -e" not in run, "no errexit here, so the per-command guards are load-bearing"

    required = [ln.strip() for ln in run.splitlines() if "cp " in ln]
    assert required, "expected a cp of the JUnit report into the staging directory"
    for target in ('"$STAGE_DIR/outcome.txt"', '"$GITHUB_OUTPUT"'):
        writes = [ln.strip() for ln in run.splitlines() if target in ln and "printf" in ln]
        assert writes, f"expected a write to {target} in the staging script"
        required += writes

    for line in required:
        assert line.endswith("|| {"), (
            f"required for a complete staging directory, so it must fail the step: {line!r}"
        )
