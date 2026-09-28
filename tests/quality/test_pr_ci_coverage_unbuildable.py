"""Issue #1782: DIRTY merge refs are unbuildable, not uncovered.

[if] an open non-docs PR head has mergeable_state dirty [then] the watchdog logs verdict=unbuildable, does not fail, and does not list it as uncovered, [else stop].
[if] the same PR is rebased clean and gains a run [then] it is no longer unbuildable and counts as covered, [else stop].
[if] a clean mergeable_state head has zero runs [then] the watchdog still fails, [else stop].
[if] the run log names an exempted PR [then] verdict=unbuildable is visible rather than silent, [else stop].

-Claude
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from scripts import pr_ci_coverage

DIRTY_HEAD = "a" * 40
CLEAN_UNCOVERED_HEAD = "b" * 40
CLEAN_COVERED_HEAD = "c" * 40
NON_DOCS_FILE = "apps/webui/backend/app.py"


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    *,
    open_prs: list[tuple[int, str, str | None]],
    covered_heads: set[str] | None = None,
) -> tuple[int, str]:
    covered_heads = covered_heads or set()

    monkeypatch.setattr(pr_ci_coverage, "_open_prs", lambda: open_prs)
    monkeypatch.setattr(pr_ci_coverage, "_recently_merged_prs", lambda _cutoff: [])
    monkeypatch.setattr(pr_ci_coverage, "_changed_files", lambda _n, _h: [NON_DOCS_FILE])
    monkeypatch.setattr(
        pr_ci_coverage,
        "_has_actions_run_at_head",
        lambda head: head in covered_heads,
    )

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        exit_code = pr_ci_coverage.main(publish_status=False)
    return exit_code, buffer.getvalue()


def test_dirty_head_is_unbuildable_not_uncovered(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, log = _run_main(
        monkeypatch,
        open_prs=[(492, DIRTY_HEAD, "dirty")],
    )
    assert exit_code == pr_ci_coverage.EXIT_OK
    assert f"pr=#492 head={DIRTY_HEAD} verdict=unbuildable" in log
    assert "uncovered=0" in log
    assert "unbuildable=1" in log
    assert "[ERROR]" not in log
    assert "has no non-bot Actions run" not in log


def test_rebased_clean_head_with_run_is_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, log = _run_main(
        monkeypatch,
        open_prs=[(492, CLEAN_COVERED_HEAD, "clean")],
        covered_heads={CLEAN_COVERED_HEAD},
    )
    assert exit_code == pr_ci_coverage.EXIT_OK
    assert f"pr=#492 head={CLEAN_COVERED_HEAD} verdict=success" in log
    assert "unbuildable=0" in log
    assert "uncovered=0" in log


def test_clean_head_without_runs_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, log = _run_main(
        monkeypatch,
        open_prs=[(513, CLEAN_UNCOVERED_HEAD, "clean")],
    )
    assert exit_code == 1
    assert f"pr=#513 head={CLEAN_UNCOVERED_HEAD} verdict=failure" in log
    assert "uncovered=1" in log
    assert f"[ERROR] PR #513 head {CLEAN_UNCOVERED_HEAD} has no non-bot Actions run" in log


def test_log_distinguishes_unbuildable_from_uncovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_code, log = _run_main(
        monkeypatch,
        open_prs=[
            (492, DIRTY_HEAD, "dirty"),
            (513, CLEAN_UNCOVERED_HEAD, "clean"),
        ],
    )
    assert exit_code == 1
    assert "verdict=unbuildable" in log
    assert "verdict=failure" in log
    assert "unbuildable=1" in log
    assert "uncovered=1" in log


def test_dirty_merge_state_skips_actions_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] mergeable_state is dirty [then] skip Actions lookup, [else stop]."""
    called = False

    def fail_if_called(_head: str) -> bool:
        nonlocal called
        called = True
        return False

    monkeypatch.setattr(pr_ci_coverage, "_changed_files", lambda _n, _h: [NON_DOCS_FILE])
    monkeypatch.setattr(pr_ci_coverage, "_has_actions_run_at_head", fail_if_called)
    inspection = pr_ci_coverage._inspect_pr((492, DIRTY_HEAD, "dirty"))
    assert inspection[4] is True
    assert called is False
