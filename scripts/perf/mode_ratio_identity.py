"""Pre- and post-capture identity gates for capture_mode_ratios (PERFMODE-15)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.perf.capture_build_identity import (
    _REPO,
    _frontend_mode,
    _verify_capturing_checkout_at,
    _verify_frontend_build_version,
)
from scripts.perf.capture_ledger import append_ledger_rows
from scripts.perf.mode_ratio_engine import EngineTarget, reverify_engine_target
from scripts.perf.trackify_leak_series import LeakSeries


def _capture_identity_reason(
    frontend: str, expected_sha: str, repo_root: Path = _REPO
) -> str | None:
    """None when the checkout at `repo_root` is clean at `expected_sha` and
    `frontend` serves that same static build; otherwise why not.

    `main()` runs this before the capture and again before any row is written
    (Sol P1/BLOCKING, PR #4540): the capture can run for an hour, and a
    checkout that moved or went dirty, or a frontend redeployed mid-run, would
    otherwise mix builds under one clean-looking sha. The checkout is checked
    first so a dirty tree refuses without touching the network.
    """
    checkout_reason = _verify_capturing_checkout_at(expected_sha, repo_root)
    if checkout_reason is not None:
        return checkout_reason
    if _frontend_mode(frontend) == "vite-dev":
        return (
            "capture_mode_ratios refuses a vite-dev frontend: /_app/version.json 404s in "
            "dev mode, so it cannot confirm its own build identity (mirrors "
            "capture_library_targets.py's vite-dev refusal, PR #4034, discussion_r4132371694)"
        )
    return _verify_frontend_build_version(frontend, expected_sha)


def _append_rows_after_reverification(
    ledger: Path,
    rows: list[dict[str, Any]],
    frontend: str,
    sha: str,
    repo_root: Path = _REPO,
    *,
    engine: EngineTarget | None,
    leak_series_out: tuple[LeakSeries, Path] | None = None,
) -> None:
    """Re-run every identity gate after sampling, then save and append; never either on a refusal.

    Sol P1/BLOCKING (PR #4034, discussion_r4149791234): the leak capture can
    run for an hour, so every identity gate runs again after sampling and
    before any row is appended, as capture_library_mode.py does. With an
    `engine`, the PERFMODE-14 target gate also runs with its pid pinned. The leak
    series TSV is evidence too, so it is written only after the gates pass (r4171071154).
    """
    post_reason = _capture_identity_reason(frontend, sha, repo_root)
    if post_reason is None and engine is not None:
        post_reason = reverify_engine_target(engine, frontend, sha, repo_root)
    if post_reason is not None:
        raise SystemExit(
            "refusing to write rows: post-capture reverification failed (checkout or "
            f"frontend changed during the capture): {post_reason}"
        )
    if leak_series_out is not None:
        leak_series_out[0].write_tsv(leak_series_out[1])
    append_ledger_rows(ledger, rows)
