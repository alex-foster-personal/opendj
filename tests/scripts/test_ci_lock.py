"""The CI locks stay in step with the requirements files they are compiled from.

CI venvs sync from hash-pinned pylock.*.toml files (scripts/ci_lock.py, issue
#4252) instead of resolving requirements.txt against the live index. A lock
that silently lags its source would test a different environment than the one
requirements.txt declares, so drift must go red here, with no network.

Requirements:
- ✔︎ Every committed lock matches its source's requirement lines and compile
  settings, and pins a version inside every direct specifier.
- ✔︎ Comment-only edits to a source are not drift.
- ✔︎ Anything the check cannot model is UNKNOWN, never a pass.
- ✔︎ The pytest lock carries the pyproject `observability` extra verbatim.

Acceptance tests:
- [if] requirements.txt gains, loses or re-pins a requirement without a
  recompile [then ⛔️] the check reports the lock stale.
- [if] only a comment changes [then ⛔️] the check still passes (overshoot control).
- [if] a lock pins a version its source's specifier excludes, or a git
  requirement at another commit [then ⛔️] the check names it, even when the
  fingerprint was hand-updated to match.
- [if] a source uses an option line the parser does not model [then ⛔️] the
  check raises Unmeasurable rather than skipping the line.
"""

from __future__ import annotations

import re
import shutil
import tomllib
from pathlib import Path

import pytest

from scripts import ci_lock
from scripts.ci_lock import LOCKS, Lock, Unmeasurable, lock_problems, source_fingerprint

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_LOCK = next(lock for lock in LOCKS if lock.output == "pylock.ci.toml")
MADMOM_SHA = "27f032e8947204902c675e5e341a3faf5dc86dae"

pytestmark = pytest.mark.requirement("INFRA-12")


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------
def _copy_lock_tree(tmp_path: Path, lock: Lock) -> Path:
    """Copy one lock plus every source file it reads into a scratch root."""
    for rel in {lock.output, lock.source, "requirements.txt"}:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, target)
    return tmp_path


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"fixture edit target {old!r} missing from {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _restamp(root: Path, lock: Lock) -> None:
    """Hand-update the fingerprint, as someone papering over drift would."""
    path = root / lock.output
    text = path.read_text(encoding="utf-8")
    fresh = source_fingerprint(root / lock.source)
    path.write_text(
        re.sub(r'source-sha256 = "[0-9a-f]+"', f'source-sha256 = "{fresh}"', text), encoding="utf-8"
    )


# -----------------------------------------------------------------------------
# the committed locks
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("lock", LOCKS, ids=[lock.output for lock in LOCKS])
def test_every_committed_lock_matches_its_source(lock: Lock) -> None:
    problems = lock_problems(lock)
    assert not problems, f"run `python -m scripts.ci_lock compile`: {problems}"


def test_check_cli_reports_every_lock_fresh(capsys: pytest.CaptureFixture[str]) -> None:
    assert ci_lock.main(["check"]) == 0, capsys.readouterr().err
    assert f"{len(LOCKS)} CI locks match" in capsys.readouterr().out


def test_pytest_lock_source_carries_the_observability_extra_verbatim() -> None:
    """requirements.txt omits sentry-sdk on purpose; the Sentry e2e tests import it."""
    extra = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]["observability"]
    lines = ci_lock.requirement_lines(REPO_ROOT / CI_LOCK.source)
    assert extra and set(extra) <= set(lines), f"{CI_LOCK.source} lacks {extra}"


def test_the_git_requirement_is_actually_compared() -> None:
    """Positive control: the madmom git pin is present, so its comparison is not vacuous."""
    doc = tomllib.loads((REPO_ROOT / CI_LOCK.output).read_text(encoding="utf-8"))
    madmom = [p for p in doc["packages"] if p["name"] == "madmom"]
    assert madmom and madmom[0]["vcs"]["commit-id"] == MADMOM_SHA


# -----------------------------------------------------------------------------
# drift: each must go red on a scratch copy
# -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "old,new",
    [
        ("rbox==0.1.7\n", "rbox==0.1.7\nsix>=1.0\n"),  # added
        ("rbox==0.1.7\n", "\n"),  # removed
        ("rbox==0.1.7\n", "rbox==0.1.6\n"),  # re-pinned
    ],
    ids=["added", "removed", "repinned"],
)
def test_requirements_edit_without_recompile_is_drift(tmp_path: Path, old: str, new: str) -> None:
    root = _copy_lock_tree(tmp_path, CI_LOCK)
    assert not lock_problems(CI_LOCK, root), "control: the untouched copy must be fresh"
    _edit(root / "requirements.txt", old, new)
    problems = lock_problems(CI_LOCK, root)
    assert any("fingerprint" in p for p in problems), problems


def test_comment_only_edit_is_not_drift(tmp_path: Path) -> None:
    """Overshoot control: requirements.txt is mostly comments, which do not resolve."""
    root = _copy_lock_tree(tmp_path, CI_LOCK)
    _edit(
        root / "requirements.txt", "rbox==0.1.7\n", "rbox==0.1.7  # trailing note\n# new line\n\n"
    )
    assert not lock_problems(CI_LOCK, root)


def test_locked_version_outside_the_specifier_is_drift_even_when_restamped(tmp_path: Path) -> None:
    root = _copy_lock_tree(tmp_path, CI_LOCK)
    _edit(root / "requirements.txt", "rbox==0.1.7\n", "rbox==0.1.6\n")
    _restamp(root, CI_LOCK)
    problems = lock_problems(CI_LOCK, root)
    assert problems and all("rbox" in p and "outside" in p for p in problems), problems


def test_git_requirement_at_another_commit_is_drift_even_when_restamped(tmp_path: Path) -> None:
    root = _copy_lock_tree(tmp_path, CI_LOCK)
    _edit(root / "requirements.txt", MADMOM_SHA, "0" * 40)
    _restamp(root, CI_LOCK)
    problems = lock_problems(CI_LOCK, root)
    assert problems and all("madmom" in p for p in problems), problems


def test_requirement_absent_from_the_lock_is_drift_even_when_restamped(tmp_path: Path) -> None:
    root = _copy_lock_tree(tmp_path, CI_LOCK)
    _edit(root / "requirements.txt", "rbox==0.1.7\n", "rbox==0.1.7\nnot-a-locked-package>=1\n")
    _restamp(root, CI_LOCK)
    assert lock_problems(CI_LOCK, root) == [
        "pylock.ci.toml: not-a-locked-package>=1: not in the lock"
    ]


# -----------------------------------------------------------------------------
# unmeasurable is never a pass
# -----------------------------------------------------------------------------
@pytest.mark.parametrize("line", ["--index-url https://example.invalid/simple", "-e ."])
def test_unmodeled_option_line_is_unmeasurable(tmp_path: Path, line: str) -> None:
    root = _copy_lock_tree(tmp_path, CI_LOCK)
    _edit(root / "requirements.txt", "rbox==0.1.7\n", f"rbox==0.1.7\n{line}\n")
    with pytest.raises(Unmeasurable, match="option line"):
        lock_problems(CI_LOCK, root)


def test_missing_lock_is_unmeasurable_and_the_cli_says_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _copy_lock_tree(tmp_path, CI_LOCK)
    (root / CI_LOCK.output).unlink()
    with pytest.raises(Unmeasurable, match="missing lock"):
        lock_problems(CI_LOCK, root)
    monkeypatch.setattr(ci_lock, "REPO_ROOT", root)
    monkeypatch.setattr(ci_lock, "LOCKS", (CI_LOCK,))
    assert ci_lock.main(["check"]) == 2
    assert "UNKNOWN" in capsys.readouterr().err


# -----------------------------------------------------------------------------
# inventory: every synced lock is checked, every checked lock is synced
# -----------------------------------------------------------------------------
def test_every_lock_ci_syncs_is_in_the_checked_inventory() -> None:
    callers = [
        *(REPO_ROOT / ".github" / "workflows").glob("*.yml"),
        *(REPO_ROOT / "scripts").glob("*.sh"),
    ]
    synced = {
        match
        for path in callers
        for match in re.findall(r"ci_venv\.sh \S+ --lock (\S+)", path.read_text(encoding="utf-8"))
    }
    inventory = {lock.output for lock in LOCKS}
    assert len(synced) >= 4, f"control: expected the four CI locks to be synced, found {synced}"
    assert synced == inventory, (
        f"synced but never drift-checked: {sorted(synced - inventory)}; "
        f"checked but never synced: {sorted(inventory - synced)}"
    )
