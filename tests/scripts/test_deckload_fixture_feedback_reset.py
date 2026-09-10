"""feedback/ must never survive across a fixture rebuild.

Codex P2 BLOCKING on PR #1628: `deckload_fixture.py` only conditionally reset
`fixture-audio/` and `state/` (gated on `FIXTURE_REVISION`) and never touched
`feedback/` at all, so a real `feedback_mark`/pin/archive write from one run
sat there forever. An interrupted `performance-feedback-card-dismiss.spec.ts`
run (killed after it seeds a pin, before its `finally` archives it) would then
poison the NEXT run: its `button.fb-pin[title^=...]` locator would match both
the stale pin and the new one, breaking Playwright's strict-mode
single-element assumption.
"""

from __future__ import annotations

from pathlib import Path

from apps.webui.frontend.tests.e2e.support.deckload_fixture import (
    FEEDBACK_SUBDIR,
    _reset_feedback_dir,
)


def test_a_stale_feedback_file_from_a_prior_run_is_removed(tmp_path: Path) -> None:
    """if feedback/ holds a file from a prior run then the next call removes it"""
    data_dir = tmp_path / "fixture-data"
    stale_pin = data_dir / FEEDBACK_SUBDIR / "performance-marks.json"
    stale_pin.parent.mkdir(parents=True)
    stale_pin.write_text('{"marks": ["stale-pin-from-an-interrupted-run"]}', encoding="utf-8")

    _reset_feedback_dir(data_dir)

    assert not (data_dir / FEEDBACK_SUBDIR).exists(), (
        "a stale feedback/ dir survived a reset, so a pin an interrupted prior "
        "run seeded would still be there to double-match the next run's locator"
    )


def test_a_missing_feedback_dir_is_not_an_error(tmp_path: Path) -> None:
    """if feedback/ does not exist yet then the reset is a silent no-op"""
    data_dir = tmp_path / "fresh-fixture-data"
    data_dir.mkdir()

    _reset_feedback_dir(data_dir)  # must not raise

    assert not (data_dir / FEEDBACK_SUBDIR).exists()


def test_the_reset_never_touches_sibling_state(tmp_path: Path) -> None:
    """if state/ and fixture-audio/ exist beside feedback/ then neither is swept

    Control for the fix above: a reset broad enough to catch feedback/ by
    wiping the whole data dir would pass the two tests above just as well,
    and would silently force a full audio/ingest rebuild on every call.
    """
    data_dir = tmp_path / "fixture-data"
    (data_dir / FEEDBACK_SUBDIR).mkdir(parents=True)
    (data_dir / FEEDBACK_SUBDIR / "archive-1.json").write_text("{}", encoding="utf-8")
    kept_state = data_dir / "state" / "state.db"
    kept_state.parent.mkdir(parents=True)
    kept_state.write_text("not-really-sqlite", encoding="utf-8")
    kept_audio = data_dir / "fixture-audio" / "a.wav"
    kept_audio.parent.mkdir(parents=True)
    kept_audio.write_bytes(b"not-really-a-wav")

    _reset_feedback_dir(data_dir)

    assert not (data_dir / FEEDBACK_SUBDIR).exists()
    assert kept_state.read_text(encoding="utf-8") == "not-really-sqlite", (
        "the feedback reset swept state/ too, forcing a needless rebuild"
    )
    assert kept_audio.read_bytes() == b"not-really-a-wav", (
        "the feedback reset swept fixture-audio/ too, forcing a needless rebuild"
    )
