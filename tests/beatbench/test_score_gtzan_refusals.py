"""What `score_gtzan.py` refuses to call a passing run (BENCH-GTZAN, PR #1660).

`_report` is the half of the script that decides whether a set of figures may
be published as a benchmark round, and every refusal in it exists because the
previous version printed the figures and exited 0 anyway. It is tested rather
than `main` because `main` imports `mir_eval`, which is a bench-only dependency
absent from the repo venv: a test that skipped for that reason would be a skip
mistaken for a pass, which is the same defect the module is about.

Regression lines:
  - if an analyzer error on a clip other than the known corrupt fixture still
    exits 0 then broken (crashing would score better than answering badly)
  - if the known corrupt GTZAN fixture becomes fatal then broken (it is the
    corpus failing, not the candidate)
  - if an UNMEASURED continuity cross-check exits 0 then broken
  - if a run with every control measured is refused then broken
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from scripts.beatbench.gates import (
    ESTABLISH_DIGEST,
    KNOWN_UNANALYZABLE_FIXTURES,
    fixtures_digest_argument,
)
from scripts.beatbench.score_gtzan import _report


def _summary(**overrides: Any) -> dict[str, Any]:
    """A summary in which every control MEASURED, so a refusal is attributable."""
    summary: dict[str, Any] = {
        "n_scored_with_empty_estimate": 0,
        "unscorable_by_reason": {"no_result": 0, "runner_error": 0, "no_reference": 0},
        "f_measure_mean": 0.891,
        "delta_vs_published": 0.002,
        "cmlt_mean": 0.797,
        "amlt_mean": 0.901,
        "our_scorer_f_mean": 0.8907,
        "our_scorer_max_abs_gap_vs_mir_eval": 0.0345,
        "our_scorer_clips_disagreeing_over_1e_3": 19,
        "offset_vs_human_median_ms": -8.0,
    }
    for name in ("cmlt", "amlt"):
        summary |= {
            f"our_{name}_n_comparable": 997,
            f"our_{name}_n_declined": 1,
            f"our_{name}_mean": 0.8,
            f"mir_eval_{name}_mean_same_clips": 0.8,
            f"our_{name}_max_abs_gap_vs_mir_eval": 0.1,
            f"our_{name}_clips_disagreeing_over_1e_6": 16,
            f"our_{name}_clips_lower_than_mir_eval": 16,
            f"our_{name}_clips_higher_than_mir_eval": 0,
        }
    return summary | overrides


def _unscorable(**overrides: list[str]) -> dict[str, list[str]]:
    return {"no_result": [], "runner_error": [], "no_reference": []} | overrides


ARGS = argparse.Namespace(shift_sweep=False, out="out.json")
PER_CLIP: dict[str, Any] = {"blues_00000": {}}
DIGEST = "0" * 64


def test_a_fully_measured_run_passes() -> None:
    """The positive control: without it, every refusal below proves nothing."""
    assert _report(ARGS, _summary(), PER_CLIP, _unscorable(), DIGEST) == 0


def test_the_known_corrupt_gtzan_fixture_is_not_fatal() -> None:
    """It is the CORPUS failing, not the candidate, and it is in the artifact."""
    unscorable = _unscorable(runner_error=sorted(KNOWN_UNANALYZABLE_FIXTURES))
    assert _report(ARGS, _summary(), PER_CLIP, unscorable, DIGEST) == 0


def test_an_unexpected_runner_error_is_fatal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Codex P1 BLOCKING, discussion_r3976348810.

    Every runner error was excluded from the denominator, so a candidate that
    CRASHES on its worst clips scored better than one that answers them badly.
    That is the abstention loophole the empty-estimate rule closes one door
    down, reopened through a different exit.
    """
    unscorable = _unscorable(runner_error=["blues_00007"])
    assert _report(ARGS, _summary(), PER_CLIP, unscorable, DIGEST) == 1
    assert "blues_00007" in capsys.readouterr().err


def test_the_allowlist_does_not_swallow_a_crash_beside_it() -> None:
    """The overshoot direction: one exempt clip must not exempt the run."""
    unscorable = _unscorable(
        runner_error=[*sorted(KNOWN_UNANALYZABLE_FIXTURES), "rock_00090"]
    )
    assert _report(ARGS, _summary(), PER_CLIP, unscorable, DIGEST) == 1


@pytest.mark.parametrize("metric", ["cmlt", "amlt"])
def test_an_unmeasured_continuity_control_is_fatal(metric: str) -> None:
    """Codex P1 BLOCKING, discussion_r3975576024.

    F stays measurable at zero when every estimate is empty while both
    continuity metrics decline on every clip, so checking only the F
    cross-check passed a run whose other two stated controls never ran.
    """
    summary = _summary(**{
        f"our_{metric}_n_comparable": 0,
        f"our_{metric}_note": "UNMEASURED: no clip produced a comparable value.",
    })
    assert _report(ARGS, summary, PER_CLIP, _unscorable(), DIGEST) == 1


def test_an_unimportable_scorer_is_reported_once_not_three_times(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The control on the nesting: one cause must not print as three symptoms."""
    summary = _summary(our_scorer_f_mean=None, our_scorer_note="UNMEASURED: no import.")
    assert _report(ARGS, summary, PER_CLIP, _unscorable(), DIGEST) == 1
    assert capsys.readouterr().err.count("[gtzan] FAIL") == 1


@pytest.mark.parametrize(
    "raw", ["", "not-a-digest", "0" * 63, "0" * 65, "Z" * 64, "ESTABLISH"]
)
def test_an_unparseable_fixtures_digest_is_refused(raw: str) -> None:
    """Codex P1 BLOCKING, discussion_r3976483262.

    The flag defaulted to the empty string, so OMITTING it disabled the gate
    and the committed reproduction command did exactly that: a truncated pairs
    manifest or a different GTZAN extraction was scored, given a freshly
    computed digest, and exited 0. It is required now, and a malformed value
    must not read as no value.
    """
    with pytest.raises(argparse.ArgumentTypeError):
        fixtures_digest_argument(raw)


@pytest.mark.parametrize("raw", [ESTABLISH_DIGEST, "a1" * 32])
def test_a_digest_and_the_explicit_establish_request_are_both_accepted(
    raw: str,
) -> None:
    """The control: the rule must still say yes to both legitimate inputs.

    A first round over a new corpus has no earlier digest, so it says
    `establish` in the command line where a reviewer can see the decision,
    rather than making it by omission.
    """
    assert fixtures_digest_argument(raw) == raw
