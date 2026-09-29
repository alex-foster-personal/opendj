"""duplication.percent measures the TRACKED tree, never untracked debris.

Tue 29 Sep 2026: the required "quality ratchet" check failed
duplication.percent (0.29-0.31 against a 0.22 baseline, +11 clones) on PRs
that touched nothing jscpd scans. Every extra clone was a pair of Cargo `.d`
files under apps/audio-engine/target/debug/deps, left on persistent
self-hosted runners by a crate that main no longer tracks. jscpd walked the
apps/ FILESYSTEM, so whatever a runner kept on disk was scored as this
repo's code. The fix hands jscpd the git-tracked file list instead.

Real git repo, real pinned jscpd, no mocks.

Regression lines:
  - if an untracked duplicated pair under apps/ moves duplication.percent then
    runner debris fails unrelated PRs, so broken
  - if a TRACKED duplicated pair does not move duplication.percent then the
    tracked-only scope silenced the metric, so broken
  - if a scope with no tracked files returns a number then an empty scan
    reports as a clean tree, so broken
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import quality_gate as qg


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _source_block(tag: str) -> str:
    """A TypeScript block past both jscpd minimums, unique per `tag`.

    Distinct tags share no token run long enough to be a clone, so two tags
    are a non-duplicated pair and one tag written twice is a duplicated pair.
    """
    lines = [
        f"export function probe_{tag}(): number {{",
        f"  const values_{tag}: number[] = [];",
        *(f"  values_{tag}.push({i} * {len(tag) + i} + {tag!r}.length);" for i in range(40)),
        f"  return values_{tag}.reduce((l, r) => l + r, 0);",
        "}",
    ]
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo whose only tracked files under apps/ are NOT duplicates."""
    _git(tmp_path, "init", "-q")
    _write(tmp_path / "apps" / "y" / "alpha.ts", _source_block("alpha"))
    _write(tmp_path / "apps" / "y" / "bravo.ts", _source_block("bravo"))
    _git(tmp_path, "add", "apps")
    return tmp_path


def _clones(metric: qg.Metric) -> int:
    return int(metric.detail.split()[0])


@pytest.mark.slow
def test_untracked_duplicates_do_not_move_duplication(repo: Path) -> None:
    """[if] an untracked duplicated pair moves duplication.percent [then broken]"""
    assert _source_block("dup").count("\n") > qg.CFG.DUP_MIN_LINES, (
        "the fixture block must exceed DUP_MIN_LINES or a silent metric could "
        "be blamed on a clone jscpd is specified to skip"
    )
    before = qg._jscpd_duplication(repo)
    assert _clones(before) == 0, (
        f"the tracked pair is meant to be distinct, but jscpd found {before.detail}"
    )

    # The incident shape exactly: Cargo dep-info files, untracked and not
    # gitignored, under a target/ dir of a crate the index does not carry.
    deps = repo / "apps" / "x" / "target" / "debug" / "deps"
    _write(deps / "a.d", _source_block("dup"))
    _write(deps / "b.d", _source_block("dup"))
    after = qg._jscpd_duplication(repo)
    assert (after.value, _clones(after)) == (before.value, 0), (
        "if untracked runner debris moves duplication.percent then PRs fail for "
        f"files they never touched: {before.value} ({before.detail}) -> "
        f"{after.value} ({after.detail})"
    )


@pytest.mark.slow
def test_tracked_duplicates_do_move_duplication(repo: Path) -> None:
    """[if] a TRACKED duplicated pair does not move duplication.percent [then broken]

    The control for the test above, built from the same bytes: the pair that
    must be invisible untracked must be counted once it is in the index, or
    the tracked-only scope passes by having stopped measuring anything.
    """
    before = qg._jscpd_duplication(repo)
    deps = repo / "apps" / "x" / "target" / "debug" / "deps"
    _write(deps / "a.d", _source_block("dup"))
    _write(deps / "b.d", _source_block("dup"))
    _git(repo, "add", "-f", "apps/x")
    after = qg._jscpd_duplication(repo)
    assert after.value > before.value and _clones(after) >= 1, (
        "if a tracked clone does not raise duplication.percent then the "
        f"tracked-only scope silenced the metric: {before.value} ({before.detail}) "
        f"-> {after.value} ({after.detail})"
    )


def test_a_scope_with_no_tracked_files_fails_loud(tmp_path: Path) -> None:
    """[if] a scope with no tracked files returns a number [then broken]"""
    _git(tmp_path, "init", "-q")
    _write(tmp_path / "apps" / "y" / "untracked.ts", _source_block("alpha"))
    with pytest.raises(RuntimeError, match="no tracked files"):
        qg._tracked_files(tmp_path, ("apps",))
