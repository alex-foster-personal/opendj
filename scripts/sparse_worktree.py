"""Sparse linked worktrees: leave agent-irrelevant media out of every NEW worktree (OPS-45).

Usage::

    python -m scripts.sparse_worktree post-checkout <prev> <new> <flag>  # the hook's entry
    python -m scripts.sparse_worktree status    # SPARSE or FULL, and what is left out
    python -m scripts.sparse_worktree sparse    # make THIS linked worktree sparse
    python -m scripts.sparse_worktree full      # restore a full checkout here
    python -m scripts.sparse_worktree install-hook  # once per clone (`just wt-sparse-hook-install`)

Why: ~70 live worktrees each carried the full 285 MB tracked tree, ~120 MB of
which is blog hero renders and docs/landscape screenshots that no code, test or
build reads (measured Thu 1 Oct 2026). `scripts/githooks/post-checkout-sparse-
worktree.sh` calls `post-checkout` from every checkout; it acts ONLY when the
previous HEAD is the null id (a fresh `git worktree add`) in a LINKED worktree.

The excluded files stay in the index as skip-worktree entries, so `git ls-files`
still lists them. Any tool that lists tracked files and then reads them must go
through `tracked_bytes` (object store for skipped entries) or
`require_materialized` (loud refusal), never a silent `is_file()` filter.

Stdlib only and Python 3.9 compatible: the hook runs the system `python3`.

REQUIREMENTS (OPS-45)
  R-1 A fresh linked worktree is sparse.                            ✔︎ ✅ 🎯
      [if a linked worktree is created by plain `git worktree add` then every
       path under CFG.PATTERNS' exclusions is absent on disk and listed `S`]
      [if the previous HEAD is any non-null id (a branch switch) then the
       sparse state is left exactly as it is]
      [if the same media lives outside the excluded trees then it stays]
  R-2 The primary checkout and opted-out worktrees are full.        ✔︎ ✅ 🎯
      [if the checkout is the primary (git dir == common dir) then nothing
       changes, even on a null previous HEAD]
      [if MDT_FULL_WORKTREE=1 then the new worktree is full, including one
       that git copied sparse patterns into from a sparse parent]
      [if MDT_FULL_WORKTREE holds anything but 1 or 0 then it is refused loudly
       and the worktree is left full]
  R-3 Readers never silently skip a skip-worktree entry.            ✔︎ ✅ 🎯
      [if a listed path is skip-worktree then tracked_bytes returns its index
       blob, byte-identical to a full checkout's file]
      [if a disk-only tool's scope holds a skip-worktree path then
       require_materialized raises naming the path and `full`]
      [if nothing in scope is skipped then require_materialized is silent]
  R-4 A setup failure never leaves an unannounced state.            ✔︎ ✅ 🎯
      [if `sparse-checkout set` fails then the worktree is rolled back to a
       verified FULL checkout and the hook exits 0 with a WARN]
      [if a FULL request (opt-out) cannot undo inherited sparse state then
       the hook exits 1 with an ERROR, and `git worktree add` reports it]
      [if the python entry exits 1 then the hook script exits 1 too]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Iterator
from enum import Enum
from pathlib import Path
from typing import NamedTuple


class CFG:
    # Non-cone sparse-checkout patterns (gitignore syntax). `/*` keeps the whole
    # tree; each `!` line drops one class of binary media. Exclusion, not
    # inclusion, is deliberate: a cone (inclusion) list would silently drop any
    # NEW top-level directory, while this list can only ever omit what it names.
    # Every excluded path was measured to have no reader outside its own prose.
    PATTERNS: tuple[str, ...] = (
        "/*",
        "!/blog/**/*.png",
        "!/blog/**/*.jpg",
        "!/blog/**/*.jpeg",
        "!/docs/landscape/**/*.png",
        "!/docs/landscape/**/*.jpg",
        "!/docs/landscape/**/*.jpeg",
    )
    OPT_OUT_ENV = "MDT_FULL_WORKTREE"
    HOOK_SOURCE = Path(__file__).resolve().parent / "githooks" / "post-checkout-sparse-worktree.sh"
    RESTORE_COMMAND = "git sparse-checkout disable"


class SparseCheckoutError(RuntimeError):
    """A tool needs files on disk that this sparse worktree does not materialize."""


class Action(Enum):
    SKIP = "skip"  # not a fresh linked worktree: leave the checkout exactly as it is
    SPARSE = "sparse"
    FULL = "full"


class TrackedEntry(NamedTuple):
    tag: str  # `git ls-files -t` status: H tracked, S skip-worktree, others rare
    mode: str
    sha: str
    rel: str

    @property
    def skipped(self) -> bool:
        return self.tag == "S"


# ----- policy (pure) --------------------------------------------------------------------


def is_null_id(object_id: str) -> bool:
    """True for git's null object id, SHA-1 (40 zeros) or SHA-256 (64 zeros)."""
    return len(object_id) in (40, 64) and set(object_id) == {"0"}


def opt_out_requested(value: str | None) -> bool:
    if value in (None, "", "0"):
        return False
    if value == "1":
        return True
    raise ValueError(f"{CFG.OPT_OUT_ENV}={value!r}: expected 1 (full worktree) or 0/unset")


def decide(previous_head: str, checkout_flag: str, *, linked: bool, opt_out: bool) -> Action:
    """What the post-checkout hook does. Only a branch checkout from the null id in a
    linked worktree is a fresh `git worktree add`; everything else is left alone."""
    fresh_linked_add = checkout_flag == "1" and is_null_id(previous_head) and linked
    if not fresh_linked_add:
        return Action.SKIP
    return Action.FULL if opt_out else Action.SPARSE


# ----- git ------------------------------------------------------------------------------


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    done = subprocess.run(["git", "-C", str(repo), *args], input=stdin, capture_output=True, check=False)
    if done.returncode != 0:
        raise subprocess.CalledProcessError(done.returncode, ["git", *args], done.stdout, done.stderr)
    return done.stdout


def is_linked_worktree(repo: Path) -> bool:
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").decode().strip())
    common = Path(_git(repo, "rev-parse", "--git-common-dir").decode().strip())
    if not common.is_absolute():
        common = repo / common
    return git_dir.resolve() != common.resolve()


def is_sparse(repo: Path) -> bool:
    done = subprocess.run(
        ["git", "-C", str(repo), "config", "--bool", "core.sparseCheckout"],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() == "true"


def tracked_entries(repo: Path, *pathspecs: str) -> list[TrackedEntry]:
    """Every index entry under `pathspecs`, with its skip-worktree tag and blob id."""
    out = _git(repo, "ls-files", "-t", "-s", "-z", "--", *pathspecs)
    entries: list[TrackedEntry] = []
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        tag, mode, sha, _stage = meta.decode().split()
        entries.append(TrackedEntry(tag, mode, sha, os.fsdecode(raw_path)))
    return entries


def skip_worktree_paths(repo: Path, *pathspecs: str) -> list[str]:
    return [entry.rel for entry in tracked_entries(repo, *pathspecs) if entry.skipped]


def require_materialized(repo: Path, rels: list[str], *, purpose: str) -> None:
    """Refuse loudly when a tool that reads from disk is pointed at skipped paths."""
    # The whole index in one call, then an intersection: passing `rels` as pathspecs
    # would glob any `*`/`[` in a filename and can overflow argv for a large scope.
    skipped = set(skip_worktree_paths(repo))
    _refuse_skipped(sorted(rel for rel in rels if rel in skipped), purpose=purpose)


def _refuse_skipped(missing: list[str], *, purpose: str) -> None:
    if missing:
        raise SparseCheckoutError(
            f"{purpose}: {len(missing)} tracked path(s) are skip-worktree in this sparse "
            f"worktree and are not on disk, so this cannot be measured here: "
            f"{', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}. "
            f"Restore a full checkout with `{CFG.RESTORE_COMMAND}` "
            f"(or `python -m scripts.sparse_worktree full`)."
        )


def iter_tracked_bytes(repo: Path, entries: list[TrackedEntry]) -> Iterator[tuple[str, bytes]]:
    """(rel, content) per entry, one at a time: the working-tree file when materialized
    (so local edits still count, as before), the index blob when skip-worktree. Never a
    silent gap. Streams, because a whole-tree reader must not hold the tree in memory."""
    reader: subprocess.Popen[bytes] | None = None
    try:
        for entry in entries:
            if not entry.skipped:
                yield entry.rel, (repo / entry.rel).read_bytes()
                continue
            if reader is None:
                reader = subprocess.Popen(
                    ["git", "-C", str(repo), "cat-file", "--batch"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                )
            assert reader.stdin is not None and reader.stdout is not None
            reader.stdin.write(f"{entry.sha}\n".encode())
            reader.stdin.flush()
            header = reader.stdout.readline().decode().split()
            if len(header) != 3 or header[0] != entry.sha or header[1] != "blob":
                raise SparseCheckoutError(f"cat-file answered {header} for {entry.rel} ({entry.sha})")
            body = reader.stdout.read(int(header[2]) + 1)
            yield entry.rel, body[:-1]
    finally:
        if reader is not None:
            reader.communicate()


def require_materialized_under(repo: Path, root: Path, *, purpose: str) -> None:
    """Refuse a disk-walking tool whose scope holds ANY skip-worktree entry. A directory
    holding only excluded files is absent from a sparse tree, so its existence check
    alone would already read differently from a full checkout."""
    try:
        rel = root.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return  # outside the repository: nothing there is tracked, so nothing is skipped
    pathspec = "." if rel == "." else f":(literal){rel}"
    _refuse_skipped([e.rel for e in tracked_entries(repo, pathspec) if e.skipped], purpose=purpose)


def tracked_bytes(repo: Path, entries: list[TrackedEntry]) -> dict[str, bytes]:
    return dict(iter_tracked_bytes(repo, entries))


# ----- actions --------------------------------------------------------------------------


def make_sparse(repo: Path) -> None:
    if not is_linked_worktree(repo):
        raise SparseCheckoutError(f"{repo} is the primary checkout; only linked worktrees go sparse")
    _git(
        repo,
        "sparse-checkout",
        "set",
        "--no-cone",
        "--stdin",
        stdin="".join(f"{pattern}\n" for pattern in CFG.PATTERNS).encode(),
    )
    listed = tuple(_git(repo, "sparse-checkout", "list").decode().split())
    if not is_sparse(repo) or listed != CFG.PATTERNS:
        raise SparseCheckoutError(f"sparse-checkout did not take: patterns read back as {listed}")


def make_full(repo: Path) -> None:
    if is_sparse(repo):
        _git(repo, "sparse-checkout", "disable")
    if is_sparse(repo) or skip_worktree_paths(repo):
        raise SparseCheckoutError(f"{repo} still has skip-worktree entries after disable")


def install_hook(repo: Path) -> Path:
    """Copy the hook to the clone's effective hooks dir (core.hooksPath honored), by COPY so
    it does not track the primary's branch. Refuses to replace a different post-checkout."""
    target = Path(_git(repo, "rev-parse", "--git-path", "hooks/post-checkout").decode().strip())
    if not target.is_absolute():
        target = repo / target
    source = CFG.HOOK_SOURCE.read_bytes()
    if target.exists() and target.read_bytes() != source:
        raise SparseCheckoutError(
            f"{target} already holds a different post-checkout hook; chain the two by hand "
            f"(call {CFG.HOOK_SOURCE.name} from it) rather than overwrite it"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source)
    target.chmod(0o755)
    return target


# ----- cli ------------------------------------------------------------------------------


_SETUP_ERRORS = (subprocess.CalledProcessError, SparseCheckoutError, OSError)


def _describe(exc: BaseException) -> str:
    detail = getattr(exc, "stderr", b"") or b""
    return f"{exc} {detail.decode(errors='replace').strip()}".strip()


def _rolled_back_to_full(repo: Path) -> bool:
    try:
        make_full(repo)
    except _SETUP_ERRORS as exc:
        print(f"[ERROR] sparse-worktree: rollback to FULL failed ({_describe(exc)})", file=sys.stderr)
        return False
    return True


def _post_checkout(repo: Path, previous_head: str, checkout_flag: str) -> int:
    """The hook entry. A failed SPARSE setup rolls back to a verified FULL checkout and
    exits 0: the worktree is then exactly what the opt-out gives, only larger, so
    `git worktree add` must not report an error for it. A failure that leaves the
    worktree in a state nobody asked for (a FULL request that did not take, or a
    rollback that did not) exits 1 naming it, and `git worktree add` returns that."""
    try:
        opt_out = opt_out_requested(os.environ.get(CFG.OPT_OUT_ENV))
    except ValueError as exc:
        print(f"[WARN] sparse-worktree: {exc}; leaving this worktree FULL", file=sys.stderr)
        opt_out = True
    action = decide(previous_head, checkout_flag, linked=is_linked_worktree(repo), opt_out=opt_out)
    try:
        if action is Action.SPARSE:
            make_sparse(repo)
            skipped = len(skip_worktree_paths(repo))
            print(
                f"[sparse-worktree] SPARSE: {skipped} media file(s) left out (restore: {CFG.RESTORE_COMMAND})",
                file=sys.stderr,
            )
        elif action is Action.FULL:
            make_full(repo)
            print(f"[sparse-worktree] FULL: {CFG.OPT_OUT_ENV}=1", file=sys.stderr)
        elif action is Action.SKIP:
            pass
    except _SETUP_ERRORS as exc:
        print(f"[WARN] sparse-worktree: {action.value} failed ({_describe(exc)})", file=sys.stderr)
        if action is Action.SPARSE and _rolled_back_to_full(repo):
            print("[WARN] sparse-worktree: rolled back to a FULL checkout", file=sys.stderr)
            return 0
        print(
            f"[ERROR] sparse-worktree: {repo} is not in the state asked for ({action.value}); "
            f"inspect with `just wt-sparse-status`, restore a full tree with `{CFG.RESTORE_COMMAND}`",
            file=sys.stderr,
        )
        return 1
    return 0


def _status(repo: Path) -> int:
    entries = tracked_entries(repo)
    skipped = [entry for entry in entries if entry.skipped]
    mode = "SPARSE" if is_sparse(repo) else "FULL"
    linked = "linked worktree" if is_linked_worktree(repo) else "primary checkout"
    print(f"{mode} ({linked}): {len(skipped)} of {len(entries)} tracked paths skip-worktree")
    if mode == "SPARSE":
        print(f"restore a full checkout: {CFG.RESTORE_COMMAND}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    hook = sub.add_parser("post-checkout", help="git post-checkout hook entry")
    hook.add_argument("previous_head")
    hook.add_argument("new_head")
    hook.add_argument("checkout_flag")
    sub.add_parser("status", help="report SPARSE or FULL and what is left out")
    sub.add_parser("sparse", help="make this linked worktree sparse")
    sub.add_parser("full", help=f"restore a full checkout ({CFG.RESTORE_COMMAND})")
    sub.add_parser("install-hook", help="install the post-checkout hook for this clone")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    if args.command == "post-checkout":
        return _post_checkout(repo, args.previous_head, args.checkout_flag)
    if args.command == "install-hook":
        print(f"[OK] post-checkout hook installed at {install_hook(repo)}")
        return 0
    if args.command == "sparse":
        make_sparse(repo)
    elif args.command == "full":
        make_full(repo)
    elif args.command != "status":
        raise AssertionError(f"unhandled command {args.command}")
    return _status(repo)


if __name__ == "__main__":
    sys.exit(main())
