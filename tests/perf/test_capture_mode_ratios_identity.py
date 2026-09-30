"""Identity gates of scripts.perf.capture_mode_ratios, exercised for real.

Codex P1/BLOCKING, PR #4553, discussion_r4150378520: the previous revision
patched the gate functions to scripted answers and stubbed the capture, so a
broken checkout or frontend check could leave it green, and its successful
write supplied a single Gig track id. These tests use no mocks: a disposable
git repo (real `git init`, commit, then dirtied or moved) stands in for the
capturing checkout, a real loopback HTTP server stands in for `--frontend`,
and the positive control is a valid four-deck capture fed through the same
row builder and post-capture reverification `main()` runs.

`main()` itself is not driven here: its first line refuses non-macOS hosts,
and its sampler needs a real Playwright Chromium for 60 s or more per mode.
It calls exactly `_capture_identity_reason` before sampling and
`_append_rows_after_reverification` after, which are what these tests cover.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts.perf import capture_mode_ratios as cmr
from scripts.perf.capture_kpi_ledger import session_meta
from tests.perf.test_capture_build_identity import _serve_frontend
from tests.perf.test_library_mode_capture_identity import (
    _commit_another_change,
    _disposable_git_repo,
    _git_head,
)

_FOUR_GIG_IDS = [f"{deck}" * 40 for deck in "abcd"]
_GIG_SAMPLE = {"footprint_mb": 1000.0, "cpu_percent": 50.0, "sample_count": 4.0}
_TRACKIFY_SAMPLE = {"footprint_mb": 400.0, "cpu_percent": 20.0, "sample_count": 4.0}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _disposable_git_repo(tmp_path)


@pytest.fixture
def frontend(repo: Path) -> Iterator[str]:
    """A real static-build frontend serving the disposable repo's own HEAD."""
    server, url = _serve_frontend(version=_git_head(repo), vite_dev=False)
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()


def _empty_ledger(tmp_path: Path) -> Path:
    ledger = tmp_path / "ledger.json"
    ledger.write_text('{\n  "entries": []\n}\n', encoding="utf-8")
    return ledger


def _four_deck_rows(sha: str) -> list[dict[str, object]]:
    return cmr._gig_baseline_rows(
        _GIG_SAMPLE, _TRACKIFY_SAMPLE, list(_FOUR_GIG_IDS), session_meta(sha=sha)
    )


def _ledger_entries(ledger: Path) -> list[dict[str, object]]:
    return json.loads(ledger.read_text(encoding="utf-8"))["entries"]


# ----- pre-capture gate -------------------------------------------------------


@pytest.mark.requirement("PERFMODE-15")
def test_identity_passes_a_clean_checkout_at_sha_behind_a_matching_static_build(
    repo: Path, frontend: str
) -> None:
    """[if] the checkout is clean at the sha and the static frontend serves it [then] no reason, [else stop]."""
    assert cmr._capture_identity_reason(frontend, _git_head(repo), repo) is None


@pytest.mark.requirement("PERFMODE-15")
def test_identity_refuses_a_dirty_capturing_checkout(repo: Path, frontend: str) -> None:
    """[if] the capturing checkout has uncommitted changes [then] refused as DIRTY, [else stop]."""
    sha = _git_head(repo)
    (repo / "README.md").write_text("edited after the commit\n", encoding="utf-8")
    reason = cmr._capture_identity_reason(frontend, sha, repo)
    assert reason is not None
    assert "is DIRTY" in reason
    assert str(repo) in reason


@pytest.mark.requirement("PERFMODE-15")
def test_identity_refuses_a_clean_checkout_that_moved_to_another_commit(
    repo: Path, frontend: str
) -> None:
    """[if] the checkout is clean but HEAD is not the labeled sha [then] refused naming both shas, [else stop]."""
    sha = _git_head(repo)
    moved_sha = _commit_another_change(repo)
    reason = cmr._capture_identity_reason(frontend, sha, repo)
    assert reason is not None
    assert f"moved off {sha} to {moved_sha}" in reason


@pytest.mark.requirement("PERFMODE-15")
def test_identity_refuses_a_frontend_serving_another_build(repo: Path) -> None:
    """[if] the static frontend's own version is not the sha [then] refused naming that version, [else stop]."""
    server, url = _serve_frontend(version="some-other-checkouts-sha", vite_dev=False)
    try:
        reason = cmr._capture_identity_reason(url, _git_head(repo), repo)
    finally:
        server.shutdown()
        server.server_close()
    assert reason is not None
    assert "some-other-checkouts-sha" in reason
    assert url in reason


@pytest.mark.requirement("PERFMODE-15")
def test_identity_refuses_a_dirty_frontend_build(repo: Path) -> None:
    """[if] the static frontend was built from a dirty tree at the sha [then] refused as DIRTY, [else stop]."""
    sha = _git_head(repo)
    server, url = _serve_frontend(version=f"{sha}-dirty", vite_dev=False)
    try:
        reason = cmr._capture_identity_reason(url, sha, repo)
    finally:
        server.shutdown()
        server.server_close()
    assert reason is not None
    assert "DIRTY frontend build" in reason


@pytest.mark.requirement("PERFMODE-15")
def test_identity_refuses_a_vite_dev_frontend(repo: Path) -> None:
    """[if] `--frontend` serves vite-dev [then] refused: that bundle cannot confirm its own identity, [else stop]."""
    server, url = _serve_frontend(version=None, vite_dev=True)
    try:
        reason = cmr._capture_identity_reason(url, _git_head(repo), repo)
    finally:
        server.shutdown()
        server.server_close()
    assert reason is not None
    assert "vite-dev" in reason


# ----- post-capture reverification and write ----------------------------------


@pytest.mark.requirement("PERFMODE-15")
def test_rows_land_when_identity_still_holds_after_a_four_deck_capture(
    tmp_path: Path, repo: Path, frontend: str
) -> None:
    """[if] a valid four-deck capture ends with every gate still passing [then] both ratio rows land naming all four decks, [else stop]."""
    sha = _git_head(repo)
    ledger = _empty_ledger(tmp_path)
    cmr._append_rows_after_reverification(ledger, _four_deck_rows(sha), frontend, sha, repo)
    entries = _ledger_entries(ledger)
    assert [row["kpi"] for row in entries] == [
        "trackify_mode_footprint_ratio",
        "trackify_mode_cpu_ratio",
    ]
    assert [row["value"] for row in entries] == [0.6, 0.6]
    for row in entries:
        assert all(stable_id in str(row["note"]) for stable_id in _FOUR_GIG_IDS)


@pytest.mark.requirement("PERFMODE-15")
def test_rows_refused_when_the_checkout_goes_dirty_during_sampling(
    tmp_path: Path, repo: Path, frontend: str
) -> None:
    """[if] the checkout goes dirty during sampling [then] refused and no row is written, [else stop]."""
    sha = _git_head(repo)
    ledger = _empty_ledger(tmp_path)
    rows = _four_deck_rows(sha)
    (repo / "harness-edit.txt").write_text("edited mid-capture\n", encoding="utf-8")
    with pytest.raises(SystemExit, match=r"post-capture reverification failed.*is DIRTY"):
        cmr._append_rows_after_reverification(ledger, rows, frontend, sha, repo)
    assert _ledger_entries(ledger) == []


@pytest.mark.requirement("PERFMODE-15")
def test_rows_refused_when_the_checkout_moves_commit_during_sampling(
    tmp_path: Path, repo: Path, frontend: str
) -> None:
    """[if] the checkout moves to another commit during sampling [then] refused and no row is written, [else stop]."""
    sha = _git_head(repo)
    ledger = _empty_ledger(tmp_path)
    rows = _four_deck_rows(sha)
    _commit_another_change(repo)
    with pytest.raises(SystemExit, match=rf"post-capture reverification failed.*moved off {sha}"):
        cmr._append_rows_after_reverification(ledger, rows, frontend, sha, repo)
    assert _ledger_entries(ledger) == []


@pytest.mark.requirement("PERFMODE-15")
def test_rows_refused_when_the_frontend_changes_during_sampling(
    tmp_path: Path, repo: Path, frontend: str
) -> None:
    """[if] the frontend serves another build by the end of sampling [then] refused and no row is written, [else stop].

    Sol P1/BLOCKING, PR #4034, discussion_r4149791234.
    """
    sha = _git_head(repo)
    ledger = _empty_ledger(tmp_path)
    rows = _four_deck_rows(sha)
    server, swapped_url = _serve_frontend(version="some-other-checkouts-sha", vite_dev=False)
    try:
        with pytest.raises(
            SystemExit, match=r"post-capture reverification failed.*some-other-checkouts-sha"
        ):
            cmr._append_rows_after_reverification(ledger, rows, swapped_url, sha, repo)
    finally:
        server.shutdown()
        server.server_close()
    assert _ledger_entries(ledger) == []


@pytest.mark.requirement("PERFMODE-15")
def test_rows_refused_for_a_capture_that_cannot_name_four_gig_decks(repo: Path) -> None:
    """[if] a capture names fewer than four Gig decks [then] no ratio row is built, [else stop]."""
    with pytest.raises(RuntimeError, match="must hold exactly 4 ids"):
        cmr._gig_baseline_rows(
            _GIG_SAMPLE, _TRACKIFY_SAMPLE, [_FOUR_GIG_IDS[0]], session_meta(sha=_git_head(repo))
        )
