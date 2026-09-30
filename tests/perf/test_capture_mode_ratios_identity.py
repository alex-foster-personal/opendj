"""Unit tests for scripts.perf.capture_mode_ratios's pre-capture identity gate.

Split out of test_capture_mode_ratios.py (quality-ratchet
file_size.over_limit_python, PR #4034), matching this PR's own precedent of
splitting PERFMODE-14 capture identity checks into their own module.

Sol P1/BLOCKING, PR #4034, discussion_r4139047412: unlike
capture_library_mode.py's PERFMODE-14 path, this capture previously labeled
its ledger rows with the LOCAL checkout's `_git_sha()` without ever
confirming the frontend serving `--frontend` was a clean, matching static
build. These tests exercise `main()`'s wiring of the three gate calls
(`_verify_capturing_checkout_clean`, `_frontend_mode`,
`_verify_frontend_build_version`), each of which has its own thorough unit
tests in test_capture_build_identity.py; these tests are about main()'s
ordering and short-circuiting, not those functions' internals.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.perf import capture_mode_ratios as cmr


def _main_argv(ledger: Path) -> list[str]:
    return ["--mode", "trackify", "--frontend", "http://127.0.0.1:5273", "--ledger", str(ledger)]


@pytest.mark.requirement("PERFMODE-15")
def test_main_refuses_a_dirty_capturing_checkout(tmp_path: Path) -> None:
    """[if] this checkout itself is dirty [then] main() refuses before probing anything, [else stop].

    Sol P1/BLOCKING, PR #4034, discussion_r4139047412: this capture never
    verified the frontend it measured or the checkout doing the measuring,
    so a dirty checkout could label a row `measured: true`. Asserts the
    frontend probes are never reached once the checkout gate fails.

    `_require_macos()` is patched out: it is main()'s own FIRST line (an
    unrelated, pre-existing gate), and this CI runner is Linux, so without
    patching it main() raises the macOS-refusal message instead of ever
    reaching the checkout gate under test here -- caught live by a red
    "pytest fast tier" leg on PR #4034, discussion_r4149112384.
    """
    with (
        patch("scripts.perf.capture_mode_ratios._require_macos"),
        patch(
            "scripts.perf.capture_mode_ratios._verify_capturing_checkout_clean",
            return_value="checkout is dirty: M scripts/perf/capture_mode_ratios.py",
        ),
        patch("scripts.perf.capture_mode_ratios._frontend_mode") as frontend_mode,
        patch("scripts.perf.capture_mode_ratios._verify_frontend_build_version") as verify_version,
        pytest.raises(SystemExit, match="checkout is dirty"),
    ):
        cmr.main(_main_argv(tmp_path / "ledger.json"))
    frontend_mode.assert_not_called()
    verify_version.assert_not_called()


@pytest.mark.requirement("PERFMODE-15")
def test_main_refuses_a_vite_dev_frontend(tmp_path: Path) -> None:
    """[if] `--frontend` serves vite-dev [then] main() refuses: that bundle cannot confirm its own identity, [else stop]."""
    with (
        patch("scripts.perf.capture_mode_ratios._require_macos"),
        patch(
            "scripts.perf.capture_mode_ratios._verify_capturing_checkout_clean", return_value=None
        ),
        patch("scripts.perf.capture_mode_ratios._frontend_mode", return_value="vite-dev"),
        patch("scripts.perf.capture_mode_ratios._verify_frontend_build_version") as verify_version,
        pytest.raises(SystemExit, match="vite-dev"),
    ):
        cmr.main(_main_argv(tmp_path / "ledger.json"))
    verify_version.assert_not_called()


@pytest.mark.requirement("PERFMODE-15")
def test_main_refuses_a_foreign_or_dirty_frontend_build(tmp_path: Path) -> None:
    """[if] the served static build's own version does not match this sha [then] main() refuses, [else stop]."""
    with (
        patch("scripts.perf.capture_mode_ratios._require_macos"),
        patch(
            "scripts.perf.capture_mode_ratios._verify_capturing_checkout_clean", return_value=None
        ),
        patch("scripts.perf.capture_mode_ratios._frontend_mode", return_value="static-build"),
        patch(
            "scripts.perf.capture_mode_ratios._verify_frontend_build_version",
            return_value="frontend serves a foreign sha",
        ),
        pytest.raises(SystemExit, match="foreign sha"),
    ):
        cmr.main(_main_argv(tmp_path / "ledger.json"))


def _run_main_with_post_capture_version(tmp_path: Path, post_reason: str | None) -> Path:
    """main() with --gig-baseline: the pre-capture gates pass, sampling is stubbed,
    and the frontend version check answers `post_reason` on its SECOND call."""
    ledger = tmp_path / "ledger.json"
    if not ledger.exists():
        ledger.write_text('{\n  "entries": []\n}\n')
    sample = {"footprint_mb": 1000.0, "cpu_percent": 50.0, "sample_count": 4.0}
    with (
        patch("scripts.perf.capture_mode_ratios._require_macos"),
        patch("scripts.perf.capture_mode_ratios._verify_capturing_checkout_clean", return_value=None),
        patch("scripts.perf.capture_mode_ratios._frontend_mode", return_value="static-build"),
        patch(
            "scripts.perf.capture_mode_ratios._verify_frontend_build_version",
            side_effect=[None, post_reason],
        ),
        patch(
            "scripts.perf.capture_mode_ratios._capture_gig_then_trackify",
            return_value=(sample, {**sample, "footprint_mb": 400.0}, ["a" * 40]),
        ),
    ):
        cmr.main([*_main_argv(ledger), "--gig-baseline"])
    return ledger


@pytest.mark.requirement("PERFMODE-15")
def test_main_refuses_rows_when_the_frontend_changes_during_sampling(tmp_path: Path) -> None:
    """[if] the served frontend no longer matches after sampling [then] main() refuses and
    writes no row [⛔️ if rows are appended under the pre-capture sha].

    Sol P1/BLOCKING, PR #4034, discussion_r4149791234.
    """
    with pytest.raises(SystemExit, match="post-capture reverification failed.*foreign sha"):
        _run_main_with_post_capture_version(tmp_path, "frontend serves a foreign sha")
    assert json.loads((tmp_path / "ledger.json").read_text())["entries"] == []


@pytest.mark.requirement("PERFMODE-15")
def test_main_appends_rows_when_identity_still_holds_after_sampling(tmp_path: Path) -> None:
    """Control: [if] every gate still passes after sampling [then] both ratio rows land."""
    ledger = _run_main_with_post_capture_version(tmp_path, None)
    kpis = [row["kpi"] for row in json.loads(ledger.read_text())["entries"]]
    assert kpis == ["trackify_mode_footprint_ratio", "trackify_mode_cpu_ratio"]
