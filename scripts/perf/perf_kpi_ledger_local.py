"""REPO_ROOT-side state for the nightly perf KPI ledger PR (issue #1506, PR #3827).

Everything the ledger publish does to THIS host's own disk, kept apart from the
gh/branch publishing in `perf_kpi_ledger_pr`: removing a leftover ledger worktree,
putting REPO_ROOT's tracked ledger back exactly as the caller had it, and the
unpublished-entry outbox that holds tonight's rows until they reach the branch.

[if] a publish fails [then] tonight's rows survive in the outbox or the tracked file,
and REPO_ROOT is restored only once they do, [else stop].

-Claude
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from scripts.perf.kpi_ledger_append import load_ledger


def _remove_worktree_if_present(repo_root: Path, worktree_dir: Path) -> None:
    """Clean up a leftover ledger worktree from a prior run.

    Fails loud on a real removal error instead of discarding it
    (claude-review, PR #3827, round 2, P2): round 1's fix still ran ``git
    worktree remove`` with ``check=False`` and then rmtree'd the directory
    unconditionally, so a locked worktree, a wrong-repo path, or a
    permissions error was thrown away exactly as before -- only the
    docstring changed. This now raises with git's own stderr for anything
    other than "not a working tree" (the ordinary case: nothing was ever
    registered there). ``git worktree prune`` now always runs first
    (claude-review, round 2, P3): the old early return on a missing
    directory meant a state dir wiped out from under git, or a directory
    removed after a crash, left the registration behind for the next
    ``git worktree add`` to trip over -- exactly the failure this function
    exists to prevent.
    """
    subprocess.run(["git", "worktree", "prune"], check=True, cwd=repo_root, capture_output=True)
    if not worktree_dir.exists():
        return
    removed = subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_dir)],
        check=False,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if removed.returncode == 0:
        return
    if "is not a working tree" not in removed.stderr:
        raise RuntimeError(f"git worktree remove {worktree_dir} failed: {removed.stderr.strip()}")
    # Refuse to recursively delete a directory this job cannot prove it owns
    # (Sol, PR #3827, P1/BLOCKING, "Refuse to recursively delete an unowned
    # worktree directory"): git no longer recognizing this path as a
    # worktree is not proof that nothing else put it there. `worktree_dir`
    # is configurable (`config.ledger_worktree`), so a misconfigured path
    # pointing at a real, unrelated directory would otherwise be silently
    # `rm -rf`'d the moment git failed to recognize it. A directory `git
    # worktree add --detach` created still carries its own `.git` FILE (not
    # a directory) pointing back at this repo's `.git/worktrees/<id>` --
    # that marker is what proves this directory was ever a worktree at all,
    # so only its presence licenses the recursive delete.
    if not (worktree_dir / ".git").is_file():
        raise RuntimeError(
            f"{worktree_dir} exists and git no longer recognizes it as a worktree, but it "
            "also lacks the .git file a real worktree checkout always has -- refusing to "
            "recursively delete a directory this job cannot prove it created"
        )
    # The .git file alone proves SOME repository's worktree, not this one's
    # (Sol, PR #3827, P1/BLOCKING, review comment 4108252015, "Refuse to
    # delete worktrees owned by another repository"): a state dir pointed at
    # another checkout's worktree also fails `git worktree remove` here as
    # "not a working tree". Only a gitdir inside THIS repository's own
    # `worktrees/` admin directory licenses the recursive delete.
    admin_root = _git_common_dir(repo_root) / "worktrees"
    gitdir = _worktree_gitdir(worktree_dir)
    if gitdir.parent != admin_root:
        raise RuntimeError(
            f"{worktree_dir} is a git worktree, but its gitdir {gitdir} is not under "
            f"{admin_root}: not this repository's worktree -- refusing to recursively delete it"
        )
    shutil.rmtree(worktree_dir)
    subprocess.run(["git", "worktree", "prune"], check=True, cwd=repo_root, capture_output=True)


def _git_common_dir(repo_root: Path) -> Path:
    """Absolute, resolved path of ``repo_root``'s shared git directory."""
    completed = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return Path(completed.stdout.strip()).resolve()


def _worktree_gitdir(worktree_dir: Path) -> Path:
    """Resolved admin directory a linked worktree's ``.git`` file points at."""
    marker = (worktree_dir / ".git").read_text(encoding="utf-8").strip()
    if not marker.startswith("gitdir: "):
        raise RuntimeError(f"{worktree_dir / '.git'} is not a worktree gitdir file: {marker!r}")
    return (worktree_dir / marker.removeprefix("gitdir: ")).resolve()


def _restore_tracked_ledger(
    repo_root: Path,
    ledger_path: Path,
    *,
    pre_run_content: str | None,
    post_run_content: str,
) -> None:
    """Put REPO_ROOT's own tracked ledger back exactly as it was before this
    job touched it, success or failure.

    BLOCKING, found by review on PR #3827 (Codex): with the default
    ``MDT_PERF_KPI_LEDGER``, `run_nightly` appends this run's rows straight
    into the CHECKED-OUT, git-tracked ``docs/perf/kpi-ledger.json`` -- the
    same file ``scripts/autoreposync.sh:852-862`` inspects before pulling.
    Leaving that local modification in place afterward serves no purpose
    except to leave `repo_root` permanently dirty, which is exactly what
    `autoreposync.sh` skips, so the FIRST scheduled run could silently stop
    this install from ever receiving another `git pull` again. Restored
    unconditionally -- success or failure -- so a publish attempt never
    leaves a side effect on `repo_root` beyond what it actually achieved
    (git history, or nothing).

    ``pre_run_content`` is the CALLER's responsibility to capture, and it
    must be read BEFORE `run_nightly` ever appends this run's own rows (Sol,
    PR #3827, P1/BLOCKING, review 4107678114, "Preserve the caller's ledger
    edit instead of checking it out"): the old version restored to
    ``git checkout -- <path>`` unconditionally, which only reproduces the
    caller's true prior state when nothing was locally uncommitted before
    this job ran. Any genuinely pre-existing uncommitted edit -- an
    operator's or agent's in-progress work on this same file -- was
    silently destroyed by that checkout exactly as much as this run's own
    new rows were on a publish failure, because `git checkout --` always
    goes back to HEAD, never to whatever the caller actually had. Writing
    back the EXACT content captured before anything ran is correct either
    way: on success, this run's own new rows are safely on the standing
    branch and the caller's own prior state (dirty or clean) is exactly
    restored; on failure, the caller's prior state is still exactly
    restored, and this run's own new rows are additionally kept in the
    outbox (see ``_archive_unpublished_ledger`` / ``update_ledger_pr``) so
    they are never the only copy of a lost measurement.

    ``post_run_content`` is the file exactly as this run left it, read just
    before the publish starts. The restore only replaces a file that still
    holds exactly that, and otherwise raises and leaves it alone (Sol, PR
    #3827, P1/BLOCKING, review comment 4108252023, "Preserve edits made
    while the ledger publish is running"): writing `pre_run_content`
    unconditionally erased any operator's or agent's edit made during the
    network-bound publish window. Leaving the file is safe on both paths:
    by the time this runs, tonight's rows are already on the branch or in
    the outbox.

    A no-op when ``ledger_path`` does not live inside `repo_root`'s working
    tree at all (an env override pointing somewhere else, as several tests
    do): nothing was written to `repo_root`'s own checkout to begin with.
    """
    try:
        ledger_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return
    current_content = ledger_path.read_text(encoding="utf-8") if ledger_path.exists() else None
    if current_content != post_run_content:
        raise RuntimeError(
            f"{ledger_path} changed while the ledger publish was running; leaving that edit in "
            "place instead of restoring over it (tonight's rows are already published or in "
            "the outbox)"
        )
    if pre_run_content is None:
        # Nothing existed at this path before this job touched the tree;
        # undo whatever it created rather than leaving a new file dirty.
        if ledger_path.exists():
            ledger_path.unlink()
        return
    ledger_path.write_text(pre_run_content, encoding="utf-8")


#: A single consolidated file holding whatever entries are still owed to the
#: standing branch, not one timestamped snapshot per failure (Sol, PR #3827,
#: P1/BLOCKING, "Retain unpublished entries until they are successfully
#: published"): an earlier version of this fix kept one timestamped snapshot
#: per failed run and pruned the OLDEST once more than 20 had piled up, so a
#: long enough streak of failures (a dead `gh` token, a permanently rejected
#: lease) discarded the very first night's measurements to make room for a
#: later night's -- exactly the data loss "unpublished" was supposed to
#: prevent. A single file that is only ever REWRITTEN with the full pending
#: set (never rotated) and only ever DELETED on a confirmed successful
#: publish has no age to prune by: it is retained until published, however
#: many nights that takes.
_OUTBOX_FILENAME = "unpublished-ledger.json"


def _load_outbox_entries(outbox_dir: Path) -> list:
    """Entries an earlier run failed to publish and still owes the branch.

    Empty when nothing is pending -- there is no failure history to read,
    or everything pending was already folded into a later successful
    publish and cleared.
    """
    outbox_path = outbox_dir / _OUTBOX_FILENAME
    if not outbox_path.exists():
        return []
    return load_ledger(outbox_path)["entries"]


def _archive_unpublished_ledger(entries: list, *, outbox_dir: Path) -> None:
    """(Re)write the outbox to hold exactly ``entries``.

    Called ONLY when this run's publish attempt did NOT succeed (Sol, PR
    #3827, P2/NON-BLOCKING, review 4107678146, "Stop archiving every
    successful publish as unpublished"): an earlier version of this fix
    archived whenever repo_root's on-disk ledger content merely differed
    from git HEAD, which is true after EVERY ordinary successful nightly
    append -- `run_nightly` writes this run's rows directly into the
    tracked file before `update_ledger_pr` is ever called -- so a
    perfectly healthy install accumulated a new outbox entry every single
    night, with nothing ever actually failing. Gating this on the
    caller's own success/failure signal instead of a content diff is what
    makes "unpublished" mean what it says.

    ``entries`` is the caller's full pending set for this attempt --
    typically this run's own new rows plus whatever `_load_outbox_entries`
    already returned -- not merely tonight's own contribution, so a second
    consecutive failure does not silently drop the first failure's rows.
    """
    outbox_dir.mkdir(parents=True, exist_ok=True)
    # Written to a sibling temp file and renamed into place (Sol, PR #3827,
    # P1/BLOCKING, review 5321908943, "Preserve nightly rows when outbox
    # persistence fails"): a direct write_text interrupted mid-write (disk
    # full, a kill) left a truncated outbox, which the next run then failed
    # to parse. The rename is atomic, so the outbox is either the previous
    # complete file or the new complete file, never a partial one.
    outbox_path = outbox_dir / _OUTBOX_FILENAME
    staging_path = outbox_dir / f"{_OUTBOX_FILENAME}.tmp"
    staging_path.write_text(json.dumps({"schema_version": 2, "entries": entries}), encoding="utf-8")
    staging_path.replace(outbox_path)


def _clear_outbox(outbox_dir: Path) -> None:
    """Remove the outbox once its entries are confirmed published (or were
    never actually pending in the first place)."""
    outbox_path = outbox_dir / _OUTBOX_FILENAME
    if outbox_path.exists():
        outbox_path.unlink()


def _new_entries_since(base_entries: list, local_entries: list) -> list:
    """Entries in ``local_entries`` whose exact content isn't already in ``base_entries``."""
    seen = {json.dumps(entry, sort_keys=True) for entry in base_entries}
    return [entry for entry in local_entries if json.dumps(entry, sort_keys=True) not in seen]
