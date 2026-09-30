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

from pathlib import Path
from unittest.mock import MagicMock, patch

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


_SHA = "a" * 40
_GIG = {"footprint_mb": 1000.0, "cpu_percent": 50.0, "sample_count": 6.0}
_TRACKIFY = {"footprint_mb": 400.0, "cpu_percent": 10.0, "sample_count": 6.0}


def _run_gig_capture(
    tmp_path: Path,
    *,
    checkout_reasons: list[str | None],
    shas: list[str],
    versions: list[str | None],
) -> tuple[SystemExit | None, MagicMock]:
    """Run main() through one Gig/Trackify capture with the identity gates scripted per call."""
    argv = [*_main_argv(tmp_path / "ledger.json"), "--gig-baseline"]
    with (
        patch("scripts.perf.capture_mode_ratios._require_macos"),
        patch(
            "scripts.perf.capture_mode_ratios._verify_capturing_checkout_clean",
            side_effect=checkout_reasons,
        ),
        patch("scripts.perf.capture_mode_ratios._git_sha", side_effect=shas),
        patch("scripts.perf.capture_mode_ratios._frontend_mode", return_value="static-build"),
        patch(
            "scripts.perf.capture_mode_ratios._verify_frontend_build_version",
            side_effect=versions,
        ),
        patch(
            "scripts.perf.capture_mode_ratios._capture_gig_then_trackify",
            return_value=(_GIG, _TRACKIFY, ["s1", "s2", "s3", "s4"]),
        ),
        patch("scripts.perf.capture_mode_ratios.append_ledger_rows") as append,
    ):
        try:
            cmr.main(argv)
        except SystemExit as exc:
            return (exc, append)
    return (None, append)


@pytest.mark.requirement("PERFMODE-15")
def test_main_writes_rows_when_identity_holds_after_the_capture(tmp_path: Path) -> None:
    """[if] every gate passes before AND after the capture [then] rows are written, [else stop].

    Positive control for the post-capture refusals below: without it, a
    reverification that always refused would pass them too.
    """
    exc, append = _run_gig_capture(
        tmp_path, checkout_reasons=[None, None], shas=[_SHA, _SHA], versions=[None, None]
    )
    assert exc is None
    append.assert_called_once()


@pytest.mark.requirement("PERFMODE-15")
@pytest.mark.parametrize(
    ("checkout_reasons", "shas", "versions", "match"),
    [
        ([None, "checkout is dirty: M x.py"], [_SHA, _SHA], [None, None], "checkout is dirty"),
        ([None, None], [_SHA, "b" * 40], [None, None], "checkout moved"),
        ([None, None], [_SHA, _SHA], [None, "frontend serves a foreign sha"], "foreign sha"),
    ],
    ids=["dirty-after", "sha-moved", "frontend-redeployed"],
)
def test_main_refuses_rows_when_identity_changes_during_the_capture(
    tmp_path: Path,
    checkout_reasons: list[str | None],
    shas: list[str],
    versions: list[str | None],
    match: str,
) -> None:
    """[if] checkout or served frontend changed during the capture [then] no row is written, [else stop].

    Sol P1/BLOCKING, PR #4540: the gates ran only before an up-to-an-hour
    capture, then every row was labeled with that pre-capture sha.
    """
    exc, append = _run_gig_capture(
        tmp_path, checkout_reasons=checkout_reasons, shas=shas, versions=versions
    )
    assert isinstance(exc, SystemExit)
    assert "post-capture reverification failed" in str(exc)
    assert match in str(exc)
    append.assert_not_called()
