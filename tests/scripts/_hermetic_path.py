"""Shared helper: hide a named tool from PATH without also hiding anything
else that happens to share its directory.

THE DEFECT CLASS THIS FIXES (PR #2624, `tests/scripts/test_dmg_preflight_oauth.py`;
same class fixed for bash in c115babb4). The naive way to simulate "this tool
is missing" is to drop every PATH directory that contains it:

    kept = [p for p in path.split(os.pathsep) if not (Path(p) / tool).exists()]

That drops the ENTIRE directory, not just the one executable. On a runner
where the hidden tool's directory also happens to hold something the script
needs before it ever reaches the check under test (bash itself, env,
coreutils, another interpreter), hiding the tool silently hides that other
thing too, and the script aborts on an unrelated precondition. The test's
outcome then depends on each runner's own PATH layout, not on the code being
tested -- exactly the "clean binary answer for a case that should be messy"
smell .claude/rules/verification.md warns about.

THE FIX. For each PATH directory, never drop it outright. If it does not
hold a hidden tool, keep it exactly as-is. If it does, substitute a
same-order shadow directory holding a symlink to every OTHER entry in it --
so a co-located tool such as bash keeps resolving from the same PATH
position, and only the named tool(s) genuinely disappear.

Callers pass their own `tmp_path` fixture so the shadow directories are
cleaned up the same way every other test tmp dir is.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def path_hiding(tmp_path: Path, *hidden_tools: str) -> str:
    """The real PATH, with every named tool genuinely absent.

    Every directory that does not provide one of ``hidden_tools`` is kept
    unmodified, in its original position. A directory that does provide one
    is replaced, in the same position, by a shadow directory symlinking
    every OTHER entry it held -- so nothing but the named tools disappears.
    """
    shadow_root = tmp_path / "hermetic-path"
    kept_dirs: list[str] = []
    for part in os.environ.get("PATH", "").split(os.pathsep):
        if not part:
            continue
        real_dir = Path(part)
        if not real_dir.is_dir():
            # A stale/nonexistent PATH entry cannot hide anything; keep it
            # as-is rather than silently dropping it (dropping entries this
            # helper was not asked to touch is exactly the bug being fixed).
            kept_dirs.append(part)
            continue
        hidden_here = {tool for tool in hidden_tools if (real_dir / tool).exists()}
        if not hidden_here:
            kept_dirs.append(part)
            continue
        shadow_dir = shadow_root / hashlib.sha1(part.encode("utf-8")).hexdigest()[:16]
        shadow_dir.mkdir(parents=True, exist_ok=True)
        for entry in real_dir.iterdir():
            if entry.name in hidden_here:
                continue
            try:
                target = entry.resolve()
            except OSError:
                continue
            try:
                link = shadow_dir / entry.name
                if entry.is_dir() and not entry.is_symlink():
                    link.symlink_to(target, target_is_directory=True)
                else:
                    link.symlink_to(target)
            except OSError:
                # A duplicate name or a permission wrinkle on one entry must
                # not take the whole directory down with it.
                continue
        kept_dirs.append(str(shadow_dir))
    return os.pathsep.join(kept_dirs)
