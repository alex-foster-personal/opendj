"""Issue #1774 / PR #1747 thread r3980257735: gate commit on main must be re-sampled before a verdict.

`review_coverage` pinned main's gate once, then several network round trips may run. If main gains a
newer gate commit meanwhile, printing PASS or FAIL would certify evidence collected under a gate
main has already replaced. These tests run the real `triage` / `main` path against real git repos;
only the GitHub API boundary is doubled.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import review_coverage, review_coverage_carry, review_gate_freshness

_PR = "1774"
_HEAD = "a" * 40


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(root: Path, rel: str, text: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(root, "add", rel)
    _git(root, "commit", "-q", "-m", f"touch {rel}")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path, str]:
    """(maintainer, pr_checkout, opening_gate_sha): main holds one gate commit at start."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    maintainer = tmp_path / "maintainer"
    subprocess.run(["git", "clone", "-q", str(origin), str(maintainer)], check=True)
    _git(maintainer, "config", "user.email", "t@example.invalid")
    _git(maintainer, "config", "user.name", "t")
    _git(maintainer, "switch", "-q", "-c", "main")
    opening = _commit(maintainer, "scripts/review_coverage.py", "v1\n")
    _git(maintainer, "push", "-q", "origin", "main")
    pr = tmp_path / "pr"
    subprocess.run(["git", "clone", "-q", str(origin), str(pr)], check=True)
    _git(pr, "config", "user.email", "t@example.invalid")
    _git(pr, "config", "user.name", "t")
    _git(pr, "switch", "-q", "-c", "af--feature")
    _commit(pr, "apps/feature.py", "x\n")
    return maintainer, pr, opening


def _wire_github(
    monkeypatch: pytest.MonkeyPatch,
    *,
    files: list[dict],
    checks: object,
    head_calls: list[object] | None = None,
) -> None:
    monkeypatch.setattr(review_coverage, "_head_sha", lambda pr: _HEAD)
    if head_calls is not None:
        calls = {"n": 0}

        def head_sha(pr: str) -> str:
            calls["n"] += 1
            hook = head_calls[calls["n"] - 1] if calls["n"] - 1 < len(head_calls) else _HEAD
            if callable(hook):
                hook()
            return _HEAD

        monkeypatch.setattr(review_coverage, "_head_sha", head_sha)

    def paginated(path: str) -> list[dict]:
        if path.endswith("/files"):
            return files
        return []

    monkeypatch.setattr(review_coverage, "_paginated_json_list", paginated)
    monkeypatch.setattr(review_coverage, "_checks", checks)
    monkeypatch.setattr(review_coverage_carry, "diff_of", lambda pr: "")


@pytest.mark.requirement("REVIEW-07")
def test_a_gate_commit_landing_mid_run_prints_no_verdict(
    repos: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] main gains a gate commit mid-run [then] exit 3 and no verdict, [else stop]."""
    maintainer, pr, opening = repos
    new_sha: list[str] = []

    def checks(_pr: str) -> list[dict]:
        new_sha.append(_commit(maintainer, "scripts/review_coverage.py", "v2\n"))
        _git(maintainer, "push", "-q", "origin", "main")
        return []

    monkeypatch.setattr(review_gate_freshness, "CHECKOUT_ROOT", pr)
    monkeypatch.setattr(review_coverage, "CHECKOUT_ROOT", pr)
    _wire_github(
        monkeypatch,
        files=[{"filename": "apps/feature.py"}],
        checks=checks,
    )
    rc = review_coverage.main(["review_coverage", _PR])
    captured = capsys.readouterr()
    assert rc == 3
    assert "PASS" not in captured.out
    assert "FAIL" not in captured.out
    assert f"[review-coverage] PR #{_PR} @" not in captured.out
    assert "COULD NOT MEASURE" in captured.err
    assert opening in captured.err
    assert new_sha[0] in captured.err


def test_no_main_move_still_renders_a_verdict(
    repos: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    maintainer, pr, _opening = repos
    del maintainer

    monkeypatch.setattr(review_gate_freshness, "CHECKOUT_ROOT", pr)
    monkeypatch.setattr(review_coverage, "CHECKOUT_ROOT", pr)
    _wire_github(
        monkeypatch,
        files=[{"filename": "apps/feature.py"}],
        checks=lambda _pr: [],
    )
    rc = review_coverage.main(["review_coverage", _PR])
    out = capsys.readouterr().out
    assert f"[review-coverage] PR #{_PR} @" in out
    assert rc in (0, 1)
    assert "did not review" in out or "PASS:" in out or "FAIL:" in out


def test_a_non_gate_commit_landing_mid_run_still_renders_a_verdict(
    repos: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    maintainer, pr, _opening = repos

    def checks(_pr: str) -> list[dict]:
        _commit(maintainer, "apps/unrelated.py", "y\n")
        _git(maintainer, "push", "-q", "origin", "main")
        return []

    monkeypatch.setattr(review_gate_freshness, "CHECKOUT_ROOT", pr)
    monkeypatch.setattr(review_coverage, "CHECKOUT_ROOT", pr)
    _wire_github(
        monkeypatch,
        files=[{"filename": "apps/feature.py"}],
        checks=checks,
    )
    rc = review_coverage.main(["review_coverage", _PR])
    out = capsys.readouterr().out
    assert f"[review-coverage] PR #{_PR} @" in out
    assert rc in (0, 1)


def test_docs_only_gate_move_before_verdict_prints_no_pass(
    repos: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    maintainer, pr, opening = repos
    new_sha: list[str] = []

    def push_gate_v2() -> None:
        new_sha.append(_commit(maintainer, "scripts/review_coverage.py", "v2\n"))
        _git(maintainer, "push", "-q", "origin", "main")

    monkeypatch.setattr(review_gate_freshness, "CHECKOUT_ROOT", pr)
    monkeypatch.setattr(review_coverage, "CHECKOUT_ROOT", pr)
    _wire_github(
        monkeypatch,
        files=[{"filename": "docs/x.md"}],
        checks=lambda _pr: [],
        head_calls=[None, push_gate_v2],
    )
    rc = review_coverage.main(["review_coverage", _PR])
    captured = capsys.readouterr()
    assert rc == 3
    assert "PASS" not in captured.out
    assert "ok docs-only" not in captured.out
    assert "COULD NOT MEASURE" in captured.err
    assert opening in captured.err
    assert new_sha[0] in captured.err
