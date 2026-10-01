"""Real-state tests for scripts.perf.capture_mode_ratios's identity gate.

`_capture_identity_reason` is what `main()` runs before the capture and again
before any ledger row is written (Sol P1/BLOCKING, PR #4034,
discussion_r4139047412, and PR #4540). Every test drives it against a
DISPOSABLE git repo it controls and a real loopback frontend server, with no
patching: AGENTS.md "No mocks and locked real fixtures" (Sol P1/BLOCKING, PR
#4540, which replaced this module's earlier patched gate answers).

`main()` itself is not driven here: past the gate it launches Playwright and
samples a live browser, which this test environment cannot run for real.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

from scripts.perf import capture_mode_ratios as cmr
from tests.perf.test_capture_build_identity import _serve_frontend
from tests.perf.test_library_mode_capture_identity import _disposable_git_repo


def _unbound_loopback_url() -> str:
    """A loopback origin nothing listens on: probing it raises instead of answering."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.mark.requirement("PERFMODE-15")
def test_a_clean_checkout_serving_its_own_static_build_passes(tmp_path: Path) -> None:
    """[if] the checkout is clean at the sha and the frontend serves that build [then] None, [else stop].

    Positive control for every refusal below: a gate that always refused would
    pass those and fail this.
    """
    repo = _disposable_git_repo(tmp_path)
    sha = cmr._git_sha(repo)
    server, url = _serve_frontend(version=sha, vite_dev=False)
    try:
        assert cmr._capture_identity_reason(url, sha, repo_root=repo) is None
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.requirement("PERFMODE-15")
def test_a_dirty_checkout_refuses_before_probing_the_frontend(tmp_path: Path) -> None:
    """[if] the checkout is dirty [then] refuse, without touching the frontend, [else stop].

    The frontend URL has no listener, so a gate that probed it before checking
    the checkout would raise instead of returning the dirty reason.
    """
    repo = _disposable_git_repo(tmp_path)
    sha = cmr._git_sha(repo)
    (repo / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
    reason = cmr._capture_identity_reason(_unbound_loopback_url(), sha, repo_root=repo)
    assert reason is not None
    assert "DIRTY" in reason


@pytest.mark.requirement("PERFMODE-15")
def test_a_checkout_moved_to_another_clean_commit_refuses(tmp_path: Path) -> None:
    """[if] the checkout moved to another clean commit mid-capture [then] refuse, naming both shas, [else stop]."""
    repo = _disposable_git_repo(tmp_path)
    sha = cmr._git_sha(repo)
    server, url = _serve_frontend(version=sha, vite_dev=False)
    try:
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "moved"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        moved = cmr._git_sha(repo)
        reason = cmr._capture_identity_reason(url, sha, repo_root=repo)
        assert reason is not None
        assert moved in reason
        assert sha in reason
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.requirement("PERFMODE-15")
def test_a_vite_dev_frontend_refuses(tmp_path: Path) -> None:
    """[if] the frontend is vite-dev [then] refuse: it cannot confirm its own identity, [else stop]."""
    repo = _disposable_git_repo(tmp_path)
    sha = cmr._git_sha(repo)
    server, url = _serve_frontend(version=None, vite_dev=True)
    try:
        reason = cmr._capture_identity_reason(url, sha, repo_root=repo)
        assert reason is not None
        assert "vite-dev" in reason
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.requirement("PERFMODE-15")
def test_a_frontend_serving_another_build_refuses(tmp_path: Path) -> None:
    """[if] the frontend serves a different build (redeployed mid-capture) [then] refuse, [else stop]."""
    repo = _disposable_git_repo(tmp_path)
    sha = cmr._git_sha(repo)
    server, url = _serve_frontend(version="some-other-build", vite_dev=False)
    try:
        reason = cmr._capture_identity_reason(url, sha, repo_root=repo)
        assert reason is not None
        assert "some-other-build" in reason
    finally:
        server.shutdown()
        server.server_close()
