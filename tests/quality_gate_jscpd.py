"""Real jscpd regression for lockfile exclusion in scripts/quality_gate.py.

Imported by tests/test_quality_gate.py so the plan-mandated commands
`pytest tests/test_quality_gate.py` and
`pytest -m slow tests/test_quality_gate.py -k 'jscpd or lockfile'`
collect this case. The body lives here because tests/test_quality_gate.py
is already 596 lines, and inlining it would cross the 600-line size gate.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import quality_gate as qg

# Representative tracked launcher source. Copied into a disposable apps/ tree
# so the real jscpd path is exercised without scanning the whole repo.
_LAUNCHER_JSCPD_SOURCE: tuple[str, ...] = (
    "apps/launcher/src/App.tsx",
    "apps/launcher/src/main.tsx",
    "apps/launcher/src/types.ts",
    "apps/launcher/src/components/Palette.tsx",
    "apps/launcher/src/components/TrackRow.tsx",
    "apps/launcher/src/components/emptyState.ts",
    "apps/launcher/src/hooks/useSearch.ts",
    "apps/launcher/src/hooks/useDragEvents.ts",
)


def _git_add(repo: Path, *paths: str) -> None:
    """Track `paths`: the gate scores the git index, not the disk."""
    subprocess.run(["git", "add", "-f", *paths], cwd=repo, check=True, capture_output=True)


def _duplicated_source_block() -> str:
    """A valid TypeScript block larger than both jscpd minimums.

    Identical copies of this in two files are the mutation that must move
    duplication.percent; if it does not, the lockfile ignore is too broad
    or jscpd is not scoring source at all.
    """
    # DUP_MIN_LINES is 30 and DUP_MIN_TOKENS is 100; 40 assignment lines of
    # several tokens each clear both with room, without depending on the
    # surrounding fixture files to pad the clone.
    lines = [
        "export function duplicatedLockfileExclusionProbe(): number {",
        "  const values: number[] = [];",
        *(f"  values.push({i} + {i} * 2);" for i in range(40)),
        "  return values.reduce((left, right) => left + right, 0);",
        "}",
    ]
    return "\n".join(lines) + "\n"


@pytest.mark.slow
def test_jscpd_ignores_lockfiles_but_still_scores_source_clones(
    tmp_path: Path,
) -> None:
    """A committed lockfile must not move duplication.percent; a source clone must.

    jscpd scores a lockfile as duplicated source because lockfiles are
    repetitive by construction. That number is not actionable, and it made
    committing the launcher lockfile a CI failure. The exclusion is the fix;
    this test is the proof it is neither a no-op nor an over-broad silence
    of the metric. Missing or broken jscpd must fail here, not skip.
    """
    lockfile = qg.REPO / "apps" / "launcher" / "pnpm-lock.yaml"
    assert lockfile.is_file(), (
        "the committed launcher lockfile must exist for this acceptance test"
    )

    for rel in _LAUNCHER_JSCPD_SOURCE:
        src = qg.REPO / rel
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    _git_add(tmp_path, "apps")
    apps = tmp_path / "apps"
    without_lock = qg._jscpd_duplication(tmp_path)
    shutil.copy2(lockfile, tmp_path / "apps" / "launcher" / "pnpm-lock.yaml")
    _git_add(tmp_path, "apps/launcher/pnpm-lock.yaml")
    with_lock = qg._jscpd_duplication(tmp_path)
    assert with_lock.value == without_lock.value, (
        "if a lockfile under apps/ moves duplication.percent then the jscpd "
        f"ignore is not covering it: {without_lock.value} -> {with_lock.value}"
    )

    block = _duplicated_source_block()
    assert block.count("\n") > qg.CFG.DUP_MIN_LINES, (
        "the mutation block must exceed DUP_MIN_LINES or a silent metric "
        "could be blamed on a clone that jscpd is specified to ignore"
    )
    (apps / "launcher" / "src" / "dup_a.ts").write_text(block, encoding="utf-8")
    (apps / "launcher" / "src" / "dup_b.ts").write_text(block, encoding="utf-8")
    _git_add(tmp_path, "apps/launcher/src")
    with_clones = qg._jscpd_duplication(tmp_path)
    assert with_clones.value > with_lock.value, (
        "if a genuine source clone does not raise duplication.percent then "
        "the lockfile exclusion also silenced the metric; "
        f"lockfile-only={with_lock.value} cloned={with_clones.value}"
    )
