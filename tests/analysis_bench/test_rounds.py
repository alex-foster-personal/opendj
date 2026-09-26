"""Round logging: the counter that lets a later session resume instead of guess.

A round is only worth appending if it can be compared with the one before it,
so these tests pin the three things that make that true: the number continues a
lane's existing counter rather than restarting it, the block names the scorer
and bundle the figures came from, and a table with no controls is refused
outright.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from apps.analysis_bench import rounds

_REPORT = {
    "lane": "beatgrid",
    "scorer_version": "1.1.0",
    "bundle": {"lane": "beatgrid", "version": "v1", "bundle_id": "abc123"},
    "table": "| candidate | n |\n|---|---|\n| beat_this | 90 |",
    "arms": {
        "beat_this": {"role": "candidate"},
        "truth_offset": {"role": "positive_control"},
        "constant_128": {"role": "negative_control"},
    },
}


def test_first_round_uses_the_lane_floor(tmp_path: Path) -> None:
    """Key starts at 0; beatgrid continues from round 1, so its floor is 2."""
    log = tmp_path / "log.md"
    log.write_text("# spec\n\n## Experiment log\n")
    assert rounds.next_round_number(log.read_text(), "key", floor=0) == 0
    assert rounds.next_round_number(log.read_text(), "beatgrid", floor=2) == 2


def test_an_existing_round_advances_the_counter(tmp_path: Path) -> None:
    text = "## Experiment log\n\n### key bench round 0\n\nbody\n\n### key bench round 1\n\nbody\n"
    assert rounds.next_round_number(text, "key", floor=0) == 2
    assert rounds.next_round_number(text, "beatgrid", floor=2) == 2


def test_append_writes_a_greppable_block(tmp_path: Path) -> None:
    log = tmp_path / "log.md"
    log.write_text("# spec\n\n## Experiment log\n\nRounds are per lane.\n")
    number = rounds.append_round(log, "beatgrid", _REPORT, floor=2, host="nucbox-wsl")
    assert number == 2
    text = log.read_text()
    assert "### beatgrid bench round 2" in text
    assert "scorer 1.1.0" in text
    assert "bundle beatgrid/v1" in text
    assert "abc123" in text
    assert "nucbox-wsl" in text
    assert text.startswith("# spec")
    assert rounds.next_round_number(text, "beatgrid", floor=2) == 3


# REQ: NATIVE-11
@pytest.mark.requirement("NATIVE-11")
def test_a_round_without_both_controls_is_refused(tmp_path: Path) -> None:
    """Spec section 6: every table carries a positive and a negative control.

    [if] a round lacks either control arm [then] it is refused unlogged, [else stop].
    """
    log = tmp_path / "log.md"
    log.write_text("## Experiment log\n")
    uncontrolled = {**_REPORT, "arms": {"beat_this": {"role": "candidate"}}}
    with pytest.raises(rounds.RoundError) as excinfo:
        rounds.append_round(log, "beatgrid", uncontrolled, floor=2, host="nucbox-wsl")
    assert "positive_control" in str(excinfo.value)
    assert "negative_control" in str(excinfo.value)
    assert log.read_text() == "## Experiment log\n"


def test_a_missing_log_is_refused_rather_than_created(tmp_path: Path) -> None:
    """A round written to a file nobody reads is a round nobody can resume."""
    with pytest.raises(rounds.RoundError):
        rounds.append_round(tmp_path / "absent.md", "key", _REPORT, floor=0, host="h")


def test_the_round_date_is_portable_to_windows() -> None:
    """`%-d` is POSIX only; Microsoft's strftime raises on it and nothing posts."""
    assert re.fullmatch(r"[A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2} \d{4}", rounds._utc_day())
    calls = [line for line in inspect.getsource(rounds).splitlines() if "strftime" in line]
    assert calls, "the date is built somewhere else now; repoint this guard at it"
    assert not any("%-" in line for line in calls), "a platform-specific directive came back"
