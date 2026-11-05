"""`queue_cli run --json` reports the batch it was asked to drain.

Sat 26 Sep 2026, hands-on retest of the installed Windows build on bifrost1:
`queue_cli --json run --batch-id qb_3528...` printed `batch_id: qb_15bf...`
with `completed: 0, skipped: 1`, while `/progress` for the requested batch
showed it done 1/1. `drain` runs the requested batch and then every cascade
batch it spawned, and the summary was the LAST of those, a key cascade, not
the requested beatgrid batch. The exit code was read off that same last
summary, so a failure in the requested batch was masked by a clean cascade.

- if the top-level `batch_id` is not the requested batch then broken
- if the top-level counts are the cascade's rather than the requested batch's
  then broken
- if a cascade batch disappears from the report then its outcome is invisible
  -- broken
- if a failure in the requested batch exits 0 because the cascade was clean
  then broken
- CONTROL (opposite direction): a failure ONLY in a cascade batch is still a
  track failure of this drain, so it must not exit 0 either
- if the drain's first summary is not the requested batch then the report
  must refuse, not relabel -- broken

-Claude
"""

from __future__ import annotations

import pytest

from apps.analysis.queue_cli import EXIT_OK, EXIT_TRACK_FAILURES, build_run_report
from apps.analysis.queue_runner import RunSummary

REQUESTED = "qb_3528"
CASCADE = "qb_15bf"


def _requested(**counts: int) -> RunSummary:
    return RunSummary(batch_id=REQUESTED, runner_id="r1", workers=1, cascade_batch_id=CASCADE, **counts)


def _cascade(**counts: int) -> RunSummary:
    return RunSummary(batch_id=CASCADE, runner_id="r1", workers=1, **counts)


def test_top_level_summary_is_the_requested_batch_not_the_last_cascade() -> None:
    payload, _ = build_run_report(REQUESTED, [_requested(completed=1), _cascade(skipped=1)])
    assert payload["batch_id"] == REQUESTED, "if the summary names a cascade batch then broken"
    assert (payload["completed"], payload["skipped"]) == (1, 0), (
        "if the requested batch's counts are replaced by the cascade's then broken"
    )
    assert payload["cascade_batch_id"] == CASCADE


def test_every_cascade_batch_is_reported_with_its_own_counts() -> None:
    payload, _ = build_run_report(REQUESTED, [_requested(completed=1), _cascade(skipped=1)])
    assert payload["drain_batches"] == 2
    assert [(b["batch_id"], b["skipped"]) for b in payload["cascade_batches"]] == [(CASCADE, 1)], (
        "if a cascade batch's outcome is missing from the report then broken"
    )


def test_a_failure_in_the_requested_batch_is_not_masked_by_a_clean_cascade() -> None:
    _, exit_code = build_run_report(REQUESTED, [_requested(failed=1), _cascade(completed=1)])
    assert exit_code == EXIT_TRACK_FAILURES


def test_control_a_failure_only_in_a_cascade_batch_still_fails_the_drain() -> None:
    _, exit_code = build_run_report(REQUESTED, [_requested(completed=1), _cascade(failed=1)])
    assert exit_code == EXIT_TRACK_FAILURES


def test_control_a_clean_drain_exits_ok() -> None:
    _, exit_code = build_run_report(REQUESTED, [_requested(completed=1), _cascade(completed=1)])
    assert exit_code == EXIT_OK


def test_a_first_summary_for_another_batch_is_refused_not_relabelled() -> None:
    with pytest.raises(ValueError, match=REQUESTED):
        build_run_report(REQUESTED, [_cascade(completed=1)])


def test_an_empty_drain_is_refused() -> None:
    with pytest.raises(ValueError, match="no batch"):
        build_run_report(REQUESTED, [])
