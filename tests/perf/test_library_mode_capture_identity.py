"""PERFMODE-14 library mode capture: pre/post-capture identity gate checks.

Split out of `test_library_mode_capture.py` (quality-ratchet
`file_size.over_limit_python`, PR #4034): five review rounds of Codex
P0/P1/BLOCKING findings each added one more identity gate (build-sha match,
vite-dev refusal, dirty-checkout refusal, then engine-pid-restart detection)
plus its own real-server test, pushing the original file past 600 lines.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.perf.test_capture_build_identity import _serve_engine, _serve_frontend


def _disposable_git_repo(tmp_path: Path) -> Path:
    """A real, throwaway git repo with one clean commit, for tests that must
    assert `_verify_capture_targets`'s checkout-clean gate against a checkout
    they fully control.

    Not the real `_REPO`: a self-hosted CI runner's persistent workspace
    (agentbox/nucbox-wsl reuse one workdir across jobs) can carry ambient
    uncommitted noise unrelated to the code under test, which broke two
    fast-tier legs for real on this PR (2026-09-29) -- exactly the gap
    `.planning/debt/4034.md` had logged as a P2. `git status --porcelain`
    only needs a real `.git`, not the project's own tree, so this is a
    genuine clean/dirty checkout rather than a fabricated result.
    """
    repo = tmp_path / "disposable-repo"
    repo.mkdir()
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "config", "user.name", "test"],
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("disposable\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True, capture_output=True
    )
    return repo


def _git_head(repo: Path) -> str:
    """The disposable repo's own HEAD, read with real git."""
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def _commit_another_change(repo: Path) -> str:
    """Move the disposable repo's HEAD to a new commit, leaving the tree clean."""
    (repo / "moved.txt").write_text("a later commit\n", encoding="utf-8")
    subprocess.run(["git", "add", "moved.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "move HEAD"], cwd=repo, check=True, capture_output=True
    )
    return _git_head(repo)


def test_main_refuses_a_capture_when_the_frontend_serves_a_different_build(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Codex P1/BLOCKING, PR #4034, discussion_r4131907730 and
    discussion_r4132371694: --engine and --frontend are genuinely independent
    origins with no proxy between them (a real static-build topology, not
    vite-dev). [if] --frontend's /_app/version.json names a DIFFERENT build
    than the capturing checkout [then] main() refuses BEFORE ever running
    Playwright, naming the frontend origin in its reason, [else] a stale or
    foreign frontend gets marked measured under a build it never served.

    No monkeypatching of `_run_playwright_capture` (Codex P1/BLOCKING,
    discussion_r4132371727): the frontend server here has no /api route at
    all, so the frontend-version gate is the ONLY thing that can make this
    request succeed or fail, and it runs strictly before
    `_run_playwright_capture` is ever reached -- a mutant that deletes the
    gate would instead crash inside `_run_playwright_capture` (no pnpm/
    playwright in this test environment), still visible as a real, unrelated
    failure rather than a silently-passed mutation.

    Skipped rather than monkeypatched on non-Darwin (Codex P1/BLOCKING, PR
    #4034, discussion_r4138055392: AGENTS.md's "No mocks and locked real
    fixtures" contract forbids monkeypatching outright, no carve-out for an
    "unrelated" gate): `_require_reference_mac` fires unconditionally
    (exit 69) before the frontend check on every non-Darwin CI runner, and
    there is no way to exercise this gate for real without the reference
    Mac; report the capability UNAVAILABLE rather than bypass the real
    production gate to force a result.
    """
    if sys.platform != "darwin":
        pytest.skip("this gate runs after the reference-Mac check, which needs Darwin")
    import scripts.perf.capture_library_mode as capture_mod

    this_sha = capture_mod._git_sha()
    engine_server, engine_url = _serve_engine(this_sha)
    frontend_server, frontend_url = _serve_frontend(
        version="some-other-checkouts-sha", vite_dev=False
    )
    try:
        exit_code = capture_mod.main(
            [
                "--engine",
                engine_url,
                "--frontend",
                frontend_url,
                "--data-dir",
                str(tmp_path),
            ]
        )
        assert exit_code == 1
        stderr = capsys.readouterr().err
        assert frontend_url in stderr
        assert "some-other-checkouts-sha" in stderr
    finally:
        engine_server.shutdown()
        engine_server.server_close()
        frontend_server.shutdown()
        frontend_server.server_close()


def test_main_refuses_a_vite_dev_frontend_outright(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Codex P1/BLOCKING, PR #4034, discussion_r4132371694: [if] --frontend is a
    vite-dev server [then] main() refuses before attempting any identity check
    against it at all, naming why (no non-proxied way to verify a dev server's
    identity) [else] a stale or dirty frontend checkout could pass by having its
    /api proxy forward to a correct engine, which verifies nothing about the
    frontend bundle itself.

    Skipped rather than monkeypatched on non-Darwin (Codex P1/BLOCKING, PR
    #4034, discussion_r4138055392: no monkeypatching, ever, per AGENTS.md):
    `_require_reference_mac` fires unconditionally (exit 69) before this
    gate on every non-Darwin CI runner, and there is no way to exercise it
    for real without the reference Mac; report UNAVAILABLE rather than
    bypass the real gate.
    """
    if sys.platform != "darwin":
        pytest.skip("this gate runs after the reference-Mac check, which needs Darwin")
    import scripts.perf.capture_library_mode as capture_mod

    this_sha = capture_mod._git_sha()
    engine_server, engine_url = _serve_engine(this_sha)
    frontend_server, frontend_url = _serve_frontend(version=None, vite_dev=True)
    try:
        exit_code = capture_mod.main(
            [
                "--engine",
                engine_url,
                "--frontend",
                frontend_url,
                "--data-dir",
                str(tmp_path),
            ]
        )
        assert exit_code == 1
        stderr = capsys.readouterr().err
        assert "vite-dev" in stderr
    finally:
        engine_server.shutdown()
        engine_server.server_close()
        frontend_server.shutdown()
        frontend_server.server_close()


def test_main_refuses_a_capture_when_the_checkout_itself_is_dirty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Codex P1/BLOCKING, PR #4034, discussion_r4132814651: [if]
    _verify_capturing_checkout_clean reports the checkout dirty [then] main()
    refuses BEFORE ever running Playwright, printing that reason, [else] a wiring
    regression could silently drop the call and let a dirty harness run anyway.

    No monkeypatching (Codex P1/BLOCKING, PR #4034, discussion_r4138055392:
    the prior revision monkeypatched `_verify_capturing_checkout_clean` to
    fabricate a dirty result and `_run_playwright_capture` to assert it is
    never reached -- fabricated application state, which AGENTS.md's "No
    mocks and locked real fixtures" contract forbids outright even for an
    internal wiring check). This test instead makes THIS checkout genuinely,
    disposably dirty: it drops one untracked probe file at the repo root
    (removed in `finally` even if an assertion fails) and calls `main()` for
    real, so `_verify_capturing_checkout_clean()` -- called with no args,
    hardcoded to the real checkout -- reports a REAL dirty reason.
    `_run_playwright_capture` is left completely real too: if the wiring
    regressed and it were reached anyway, it would fail for real (no pnpm/
    playwright in this test environment, same reasoning as the sibling
    frontend-version test above), a genuine and visible failure rather than
    a silently-passed mutation. `_verify_capturing_checkout_clean` itself is
    exercised against a disposable temp git repo, independently of `main()`,
    in the two tests above this one.

    Skipped rather than monkeypatched on non-Darwin, for the same
    `_require_reference_mac` reason as the two tests above.
    """
    if sys.platform != "darwin":
        pytest.skip("this gate runs after the reference-Mac check, which needs Darwin")
    import scripts.perf.capture_library_mode as capture_mod

    this_sha = capture_mod._git_sha()
    engine_server, engine_url = _serve_engine(this_sha)
    frontend_server, frontend_url = _serve_frontend(version=this_sha, vite_dev=False)
    probe = capture_mod._REPO / "CAPTURE_LIBRARY_MODE_DIRTY_CHECKOUT_TEST_PROBE.tmp"
    assert not probe.exists(), f"{probe} already exists; a prior run of this test did not clean up"
    probe.write_text("disposable, removed in this test's finally block\n", encoding="utf-8")
    try:
        exit_code = capture_mod.main(
            [
                "--engine",
                engine_url,
                "--frontend",
                frontend_url,
                "--data-dir",
                str(tmp_path),
            ]
        )
        assert exit_code == 1
        stderr = capsys.readouterr().err
        assert "is DIRTY" in stderr
        assert str(capture_mod._REPO) in stderr
    finally:
        probe.unlink(missing_ok=True)
        engine_server.shutdown()
        engine_server.server_close()
        frontend_server.shutdown()
        frontend_server.server_close()


def test_verify_capture_targets_catches_the_checkout_going_dirty_between_two_calls(
    tmp_path: Path,
) -> None:
    """Codex P1/BLOCKING, PR #4034, discussion_r4138297594: the identity and
    checkout-cleanliness gates only prove the setup was correct BEFORE
    Playwright starts. A real capture dwells for minutes, so `main()` now
    calls `_verify_capture_targets` a SECOND time right after Playwright
    finishes, before any row is built. [if] the checkout is clean on the
    first call and goes dirty before the second [then] the second call
    reports the SAME dirty reason the pre-capture gate would have, [else] a
    capture that started clean and was left dirty mid-run gets appended
    under `app_build_sha` anyway.

    This drives `_verify_capture_targets` directly, twice, against a real
    loopback engine/frontend pair and a DISPOSABLE git repo this test fully
    controls (`_disposable_git_repo`, passed as `repo_root`) -- no
    monkeypatching, no Playwright, and no `_require_reference_mac` gate to
    skip on non-Darwin, since `_verify_capture_targets` itself never calls
    it. Not the real `_REPO`: that broke this exact test on two self-hosted
    CI legs (2026-09-29) when the runner's reused workspace carried ambient
    uncommitted noise unrelated to this test, the gap already logged in
    `.planning/debt/4034.md`.
    """
    import scripts.perf.capture_library_mode as capture_mod

    repo_root = _disposable_git_repo(tmp_path)
    this_sha = _git_head(repo_root)
    engine_server, engine_url = _serve_engine(this_sha)
    frontend_server, frontend_url = _serve_frontend(version=this_sha, vite_dev=False)
    probe = repo_root / "CAPTURE_LIBRARY_MODE_REVERIFY_TEST_PROBE.tmp"
    try:
        first_reason, first_mode, first_pid = capture_mod._verify_capture_targets(
            engine_url, frontend_url, this_sha, repo_root=repo_root
        )
        assert first_reason is None, f"first call should pass clean, got: {first_reason}"
        assert first_mode is not None
        assert first_pid is not None

        probe.write_text("disposable, removed with the whole tmp_path repo\n", encoding="utf-8")

        second_reason, second_mode, second_pid = capture_mod._verify_capture_targets(
            engine_url,
            frontend_url,
            this_sha,
            expected_engine_pid=first_pid,
            repo_root=repo_root,
        )
        assert second_reason is not None, "checkout went dirty between calls; reverify must refuse"
        assert "DIRTY" in second_reason
        assert second_mode is None
        assert second_pid is None
    finally:
        engine_server.shutdown()
        engine_server.server_close()
        frontend_server.shutdown()
        frontend_server.server_close()


def test_verify_capture_targets_catches_a_same_sha_engine_restart(tmp_path: Path) -> None:
    """Codex P1/BLOCKING, PR #4034, discussion_r4138422256: [if] the engine restarts
    mid-capture but keeps serving the SAME clean sha [then] the post-capture
    reverification still refuses, naming both pids, [else] a same-sha restart
    (a redeploy, a crash and supervisor respawn) passes the sha/dirty checks
    again while genuinely swapping the process the sampler measured,
    silently mixing two process lifetimes into one row.

    Simulated with two real, separate loopback engine servers both serving
    `this_sha` clean but different `pid` values -- standing in for "the same
    engine, before and after it restarted" without needing to actually
    restart a process mid-test. The checkout-clean gate runs against a
    DISPOSABLE git repo (`_disposable_git_repo`), not the real `_REPO`: see
    the sibling reverify test above for why (this test failed for the same
    reason, on the same two CI legs, 2026-09-29).
    """
    import scripts.perf.capture_library_mode as capture_mod

    repo_root = _disposable_git_repo(tmp_path)
    this_sha = _git_head(repo_root)
    frontend_server, frontend_url = _serve_frontend(version=this_sha, vite_dev=False)
    engine_before, engine_before_url = _serve_engine(this_sha, pid=11111)
    try:
        _reason, _mode, pinned_pid = capture_mod._verify_capture_targets(
            engine_before_url, frontend_url, this_sha, repo_root=repo_root
        )
        assert _reason is None, f"expected a clean pass, got: {_reason}"
        assert pinned_pid == 11111
    finally:
        engine_before.shutdown()
        engine_before.server_close()

    engine_after, engine_after_url = _serve_engine(this_sha, pid=22222)
    try:
        reason, mode, pid = capture_mod._verify_capture_targets(
            engine_after_url,
            frontend_url,
            this_sha,
            expected_engine_pid=pinned_pid,
            repo_root=repo_root,
        )
        assert reason is not None
        assert "11111" in reason
        assert "22222" in reason
        assert mode is None
        assert pid is None
    finally:
        engine_after.shutdown()
        engine_after.server_close()
        frontend_server.shutdown()
        frontend_server.server_close()
        frontend_server.shutdown()
        frontend_server.server_close()


def test_verify_capture_targets_catches_the_checkout_moving_to_another_commit(
    tmp_path: Path,
) -> None:
    """[if] the capturing checkout stays clean but HEAD moves to another commit between the
    two calls [then] the second call refuses, naming both shas, [else stop].

    Codex P1/BLOCKING, PR #4553, discussion_r4150378530: a dirty-only gate passes a clean
    tree at the wrong commit, so the harness could run code the ledger's app_build_sha
    never named. Real disposable repo, real loopback engine and frontend.
    """
    import scripts.perf.capture_library_mode as capture_mod

    repo_root = _disposable_git_repo(tmp_path)
    this_sha = _git_head(repo_root)
    engine_server, engine_url = _serve_engine(this_sha)
    frontend_server, frontend_url = _serve_frontend(version=this_sha, vite_dev=False)
    try:
        first_reason, _mode, first_pid = capture_mod._verify_capture_targets(
            engine_url, frontend_url, this_sha, repo_root=repo_root
        )
        assert first_reason is None, f"first call should pass clean, got: {first_reason}"

        moved_sha = _commit_another_change(repo_root)

        reason, mode, pid = capture_mod._verify_capture_targets(
            engine_url, frontend_url, this_sha, expected_engine_pid=first_pid, repo_root=repo_root
        )
        assert reason is not None, "HEAD moved between calls; reverify must refuse"
        assert f"moved off {this_sha} to {moved_sha}" in reason
        assert mode is None
        assert pid is None
    finally:
        engine_server.shutdown()
        engine_server.server_close()
        frontend_server.shutdown()
        frontend_server.server_close()
