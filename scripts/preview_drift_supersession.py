"""Commit-supersession checks for the preview-branch drift check (DEVOPS-05).

Whether a preview-only commit's OWN content still survives anywhere in the
currently-served tree -- distinct from ``scripts/preview_drift_containment.py``'s
question of whether main ever held a given combination of paths. Split out of
that module so no file in the set crosses the 600-line file-size ratchet; see
``scripts/preview_drift_core.py`` for how the two combine in ``evaluate()``.
"""
from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

from scripts.preview_drift_containment import _touched_paths
from scripts.preview_drift_git import PreviewOnlyCommit, _git, _trees_identical


def _commit_owns_a_surviving_line(cwd: Path, sha: str, preview_ref: str, path: str) -> bool:
    """True if ``git blame`` attributes at least one line of ``path``, AS
    CURRENTLY SERVED on ``preview_ref``, to ``sha``.

    ``git blame`` tracks survival at line granularity, which is the level a
    single commit's own diff actually operates at (r3975092354): a commit
    touching TWO lines of the SAME file, later followed by a fresh edit to
    only ONE of them, leaves the other line still blamed on the original
    commit -- exactly "this line is still exactly what I produced, and has
    done nothing but age since" -- even though the file as a WHOLE no longer
    matches the commit's post-state. A whole-file comparison cannot express
    that; blame already tracks it, because tracking survival through partial
    edits is blame's own job.

    A missing path (deleted since, or never existing on ``preview_ref``)
    fails the blame call outright: nothing of a deleted file's content can
    survive, so a failed measurement here correctly reads as "no surviving
    line" rather than as a finding to render.

    Deliberately NOT the only check ``_mark_superseded_commits`` runs, only a
    finer-grained ADDITION to the whole-path identical check: blame's own
    algorithm follows a pure rename (100% content match, no line actually
    changed) straight through to the PRE-rename commit, so a commit whose
    only content is a straight ``git mv`` is blamed on its PARENT, never on
    itself, even though the rename is entirely its own, un-reverted doing.
    ``_trees_identical`` catches that case directly (both the pre- and
    post-rename path compare equal to their own current state, unaffected by
    which commit blame credits), so the two checks cover each other's blind
    spot rather than one replacing the other.

    ``errors="replace"`` (r3975509874): a binary asset's blob bytes ride
    along in ``--porcelain`` output next to the attribution lines this
    function actually reads, and a strict UTF-8 decode raises on the first
    invalid byte -- crashing ``evaluate()`` before it can render ANY
    verdict, for a path this check does not even need to read the CONTENT
    of. Lossy decoding is safe here because only the ``<sha> `` prefix of
    each attribution line is inspected; a mangled content byte cannot turn
    into a false attribution match.
    """
    proc = subprocess.run(
        ["git", "blame", "--porcelain", preview_ref, "--", path],
        cwd=cwd,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        return False
    prefix = f"{sha} "
    return any(line.startswith(prefix) for line in proc.stdout.splitlines())


def _file_content_at(cwd: Path, ref: str, path: str) -> str | None:
    """``path``'s raw content at ``ref``, or ``None`` when it does not exist
    there (deleted, or not yet added) -- distinct from an existing empty
    file, which content-count comparisons must not conflate with absence.

    ``errors="replace"`` (r3975509874): a binary asset's bytes are not
    valid UTF-8 in general, and a strict decode would crash ``evaluate()``
    before it can render ANY verdict. The replacement is applied identically
    to the parent, commit, and current reads that feed
    ``_commit_deletion_still_absent``'s line-count comparison, so a binary
    path's counts stay internally consistent even though they are no longer
    a faithful line-by-line reading of the original bytes.
    """
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=cwd,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


def _commit_deletion_still_absent(cwd: Path, sha: str, preview_ref: str, path: str) -> bool:
    """True if ``sha`` reduced how many times some line appears in ``path``,
    relative to at least one parent, and ``path`` as currently served on
    ``preview_ref`` still has FEWER occurrences of that line than the parent
    did -- the reduction has not been fully undone by a later commit.

    Neither of the two checks above can see a surviving DELETION (r3975194227):
    ``_trees_identical`` needs the WHOLE path to still match this commit's
    post-state, which a later, unrelated edit to the same path breaks, and
    ``_commit_owns_a_surviving_line`` can only credit a commit for a line
    that still EXISTS to be blamed -- a removed line exists nowhere for
    ``git blame`` to attribute. A deletion whose removed content has not been
    reintroduced is still this commit's own un-landed effect and must not be
    waved through as superseded just because the file around it kept moving.

    Counted by MULTISET, not set membership (r3975388110): a plain "is this
    line's text present anywhere in the current file" check cannot tell a
    genuine restoration from an unrelated surviving DUPLICATE -- deleting one
    `x` out of `x, x, y` and then editing `y` elsewhere leaves one `x` behind
    that a set-membership check reads as "the deleted `x` is back", when it
    is really the sibling occurrence this commit never touched. Comparing
    per-line counts against the immediate parent's counts is exact regardless
    of how many duplicates exist.

    A merge can remove different content relative to each parent, so both
    are checked, matching ``_touched_paths``'s own union-not-intersection
    treatment of merges.
    """
    def _line_counts(ref: str) -> Counter[str]:
        content = _file_content_at(cwd, ref, path)
        return Counter(content.splitlines()) if content is not None else Counter()

    parents = _git(cwd, "rev-parse", f"{sha}^@").split()
    candidates = parents if len(parents) == 2 else [f"{sha}~1"]
    current_counts = _line_counts(preview_ref)
    commit_counts = _line_counts(sha)
    for parent in candidates:
        parent_counts = _line_counts(parent)
        for line, parent_count in parent_counts.items():
            if commit_counts[line] < parent_count and current_counts[line] < parent_count:
                return True
    return False


def _ls_tree_mode(cwd: Path, ref: str, path: str) -> str | None:
    """``path``'s git file mode at ``ref`` (e.g. ``100644``, ``100755``,
    ``120000`` for a symlink), or ``None`` when the path does not exist
    there.
    """
    proc = subprocess.run(
        ["git", "ls-tree", ref, "--", path],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.split()[0]


def _commit_mode_change_still_in_effect(cwd: Path, sha: str, preview_ref: str, path: str) -> bool:
    """True if ``sha`` changed ``path``'s git mode (a ``chmod +x``, or a
    regular-file/symlink type change) relative to at least one parent, and
    the mode ``sha`` set is still what ``preview_ref`` currently serves.

    None of the three checks above look at mode at all (r3975388097): they
    compare CONTENT (blob bytes or line multiplicities), and a mode-only
    commit has an EMPTY content diff by definition, so every one of them
    correctly finds nothing to attribute -- while the flipped mode is still
    being served and never reached main. Scoped to "still exactly the mode
    this commit set", not "differs from the parent's mode", so a LATER
    commit changing the mode again correctly stops crediting survival to
    this one, the same self-limiting shape the content checks already have.
    """
    parents = _git(cwd, "rev-parse", f"{sha}^@").split()
    candidates = parents if len(parents) == 2 else [f"{sha}~1"]
    current_mode = _ls_tree_mode(cwd, preview_ref, path)
    if current_mode is None:
        return False
    commit_mode = _ls_tree_mode(cwd, sha, path)
    if commit_mode is None or commit_mode != current_mode:
        return False
    for parent in candidates:
        parent_mode = _ls_tree_mode(cwd, parent, path)
        if parent_mode is not None and parent_mode != commit_mode:
            return True
    return False


def _mark_superseded_commits(
    cwd: Path, preview_ref: str, commits: list[PreviewOnlyCommit]
) -> None:
    """Flag a preview-only commit as ``superseded`` when none of its own
    content survives anywhere in the served tree (r3975388097 and
    r3975388110, generalizing r3975194227, r3975092354, r3974912531 and
    r3975002596): not reverted to its pre-state, not overwritten wholesale
    by something else, not partially overwritten while a sibling line or
    sibling path it also touched still serves exactly what it produced, not
    a still-in-effect DELETION whose removed content has not been
    reintroduced, and not a still-in-effect MODE change.

    A path survives, for this commit, when ANY of four checks holds: the
    whole path still matches the commit's post-state (``_trees_identical``,
    needed for a pure rename -- see ``_commit_owns_a_surviving_line``'s
    docstring), ``git blame`` still attributes at least one of the path's
    CURRENT lines to this commit (needed for a partial, same-file overwrite
    the whole-path check alone cannot see), the commit reduced some line's
    count and the current content still has fewer of it than the commit's
    own parent did (needed for a deletion, including of a DUPLICATE line --
    see ``_commit_deletion_still_absent``'s docstring), or the commit
    changed the path's git mode and the current mode still matches what the
    commit set (needed for a mode-only change like ``chmod +x``, which has
    an empty CONTENT diff and so is invisible to the first three checks --
    see ``_commit_mode_change_still_in_effect``'s docstring). A commit
    touching multiple lines or multiple paths is superseded only when EVERY
    one of them fails ALL FOUR checks -- never when just one line, one
    deletion, one mode change, or one path of several still traces back to
    it by any one measure.

    An EMPTY diff (an ``--allow-empty`` commit, or any commit whose net
    effect is a no-op) is the degenerate case of "nothing of it remains": it
    never served any content to begin with, so it is vacuously superseded.
    ``paths`` being empty must not fall through to the "" pathspec of
    ``_trees_identical``, which compares the ENTIRE tree rather than nothing
    -- the wrong, much broader question.
    """
    for commit in commits:
        paths = _touched_paths(cwd, commit.sha)
        if not paths or not any(
            _trees_identical(cwd, commit.sha, preview_ref, path)
            or _commit_owns_a_surviving_line(cwd, commit.sha, preview_ref, path)
            or _commit_deletion_still_absent(cwd, commit.sha, preview_ref, path)
            or _commit_mode_change_still_in_effect(cwd, commit.sha, preview_ref, path)
            for path in paths
        ):
            commit.superseded = True
