"""The CI Insights artifact hand-off between the `test` job and the insights job.

Split out of tests/scripts/test_ci_mergify_insights.py (which holds the
per-step wiring of the upload, the token probe and the verdict guard) so that
module stays under the repository's 600-line file cap. Same helpers, same
ci.yml, same regression style; only the two artifact-transfer checks live here.

Regression lines:
  - if the artifact name repeats across attempts then a re-run's isolated job
    downloads attempt 1's report and records it as the current attempt's
  - if a download step names a different shard's artifact than the upload
    template produces then it finds nothing or the wrong report, silently,
    on a continue-on-error step
  - if a failed transfer reads as a missing report then the instrumentation
    dies silently behind a green step that measured nothing
"""

from __future__ import annotations

from tests.scripts.test_ci_mergify_insights import (
    DOWNLOAD_STEP_ID,
    INSIGHTS_SHARDS,
    SHARD_EXPR,
    SHARD_JOB,
    _guard_step,
    _insights_id,
    _step_by_id,
    _steps,
)


def test_the_artifact_name_is_scoped_to_the_run_attempt() -> None:
    """if the name repeats across attempts then a re-run reads attempt 1."""
    # v4 artifacts are immutable by name and a re-run keeps the same run_id,
    # so a name without run_attempt makes the second attempt's upload fail --
    # non-fatally, by design -- leaving the isolated job to download attempt
    # 1's artifact and record it as the current attempt's result.
    upload = next(
        st for st in _steps(SHARD_JOB)
        if str(st.get("uses", "")).startswith("actions/upload-artifact@")
        and "ci-insights" in str(st.get("with", {}).get("name", ""))
    )
    up_name = upload["with"]["name"]
    assert "github.run_attempt" in up_name, (
        f"The uploaded artifact is named {up_name!r}, which repeats across "
        "attempts of the same run."
    )
    assert SHARD_EXPR in up_name, (
        f"The uploaded artifact name {up_name!r} no longer varies per shard "
        f"(the `test` job is still a real strategy.matrix), so all five "
        "shards would upload to the same artifact name and race."
    )
    # The insights job has no matrix of its own since round 2, so each of its
    # five download steps must name the LITERAL shard the upload template
    # would produce for that shard -- not the unresolved matrix expression.
    downloads = {
        shard: _step_by_id(_insights_id(DOWNLOAD_STEP_ID, shard))
        for shard in INSIGHTS_SHARDS
    }
    # The upload template names the shard via the `test` job's OWN matrix
    # expression; each download must name the LITERAL substitution of that
    # expression for its own shard, since the insights job no longer has a
    # matrix of its own to resolve it at runtime.
    assert "${{ matrix.shard }}" in up_name, (
        f"The uploaded artifact name {up_name!r} does not carry the exact "
        "'${{ matrix.shard }}' expression this test substitutes; re-derive "
        "the expected download name if that expression's spelling changed."
    )
    seen_names = set()
    for shard, step in downloads.items():
        assert str(step.get("uses", "")).startswith("actions/download-artifact@"), (
            f"download-{shard} is not an actions/download-artifact step: {step}"
        )
        down_name = step["with"]["name"]
        expected = up_name.replace("${{ matrix.shard }}", str(shard))
        assert down_name == expected, (
            f"download-{shard} asks for {down_name!r}, which does not match "
            f"what the `test` job's upload template {up_name!r} produces for "
            f"shard {shard} ({expected!r}). They have to name the SAME "
            "artifact or the download either finds nothing or finds another "
            "shard's report, and both failures are silent on a "
            "continue-on-error step."
        )
        assert "github.run_attempt" in down_name, (
            f"download-{shard} asks for {down_name!r}, which does not scope "
            "to the run attempt and can read an earlier attempt's artifact."
        )
        seen_names.add(down_name)
    assert len(seen_names) == len(INSIGHTS_SHARDS), (
        f"download names collide across shards: {seen_names}. Each shard "
        "must download its OWN artifact, not another shard's."
    )


def test_a_transfer_failure_is_not_reported_as_a_missing_report() -> None:
    """if download failure reads as absence then instrumentation dies silently."""
    guard = _guard_step()
    env = guard.get("env", {})
    for shard in INSIGHTS_SHARDS:
        key = f"DOWNLOAD_{shard}"
        assert key in env, (
            f"The guard step does not read shard {shard}'s download outcome "
            f"({key} is missing), so a failed transfer, a suppressed staging "
            "step and a shard that genuinely produced no report all arrive "
            "as HAS_REPORT=false and are annotated identically. Announcing "
            "a timeout as the expected cause of an absence that was never "
            "measured is a green step certifying an unmeasured subject."
        )
        download_id = _insights_id(DOWNLOAD_STEP_ID, shard)
        assert f"steps.{download_id}.outcome" in env[key], (
            f"{key} is {env[key]!r}, which does not read shard {shard}'s own "
            f"download step's outcome (steps.{download_id}.outcome). An "
            "expression naming a step that does not exist, or another "
            "shard's step, resolves to an empty string or the wrong shard's "
            "result, and the guard would then treat every run as a transfer "
            "failure or none of them, silently either way."
        )
    run = guard["run"]
    # The two states must produce DIFFERENT annotation severities, or reading
    # them apart in the code buys nothing for the person reading the log.
    assert "UNMEASURED" in run, (
        "The guard does not raise a distinct UNMEASURED annotation for a "
        "failed transfer. A tool that could not measure must say so rather "
        "than render its failure as a finding."
    )
    unmeasured_at = run.find("UNMEASURED")
    notice_at = run.find("no report (shard")
    assert unmeasured_at < notice_at, (
        "The legitimate-absence notice is emitted before the transfer-failure "
        "check, so it claims the absence is expected without having "
        "established that anything was actually measured."
    )
