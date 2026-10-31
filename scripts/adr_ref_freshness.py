"""Repository-ref freshness probes for :mod:`scripts.adr_check` (issue #3513)."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

STALE_MERGE_REF_MSG = "merge ref is stale, update your branch"
FRESH_ENOUGH_MSG = "base moved since the merge ref was cut, but not under"
DEFAULT_GATED_DIR = "(private path)"


@dataclass(frozen=True)
class RefVerdict:
    code: int
    message: str


def _git_rev_parse(ref: str, repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git rev-parse {ref} failed rc={proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def emit_gate_result(code: int, message: str) -> int:
    stream = sys.stdout if code == 0 else sys.stderr
    print(message, file=stream)
    return code


def _git_lines(args: list[str], repo_root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed rc={proc.returncode}: {proc.stderr.strip()}"
        )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _is_unnumbered_placeholder(
    path: str, added: set[str], modified: set[str], head_names: set[str]
) -> bool:
    """True for an ``ADR-NEW-<slug>.md`` base touched without allocating a number.

    Such a file has no number, so no renumber on main can have touched it, and
    the duplicate-id scan this guard protects has nothing stale to report about
    it: the numeric scan skips ``ADR-NEW-`` names outright, the renumber scan
    never matches one against ``ADR-\\d{4}-``, and the slug scan reads the NAME
    only. A content edit to a placeholder therefore cannot change any verdict
    this gate can produce -- measured 2026-10-17 on PR #7278 (head 98ab7eee,
    base e1e89b06): the only ``(private path)/`` movement was an ``M`` to
    ``ADR-NEW-cloudsync-identified-brick-recovery.md``, prose only, while the
    head tree still carried the same file, so the stale-ref guard failed a PR
    whose own evaluation returned ``OK -- no gated paths``.

    Two shapes are let through, both keeping the head's own copy intact:

    * base ADDED it and the head does not carry the name -- a fresh placeholder;
    * base EDITED the copy the fork point already had and the head still
      carries that same name -- a prose edit to a shared placeholder.

    Two shapes stay "moved", because the head changed what the directory means:

    * base ADDED the name and the head carries it too -- a possible slug
      collision;
    * base EDITED it and the head renamed it away -- the head allocated a
      number, which is renumber territory.
    """
    name = Path(path).name
    if not (name.startswith("ADR-NEW-") and name.endswith(".md")):
        return False
    if path in added:
        return name not in head_names
    if path in modified:
        return name in head_names
    return False


def check_ref_freshness(
    base_ref: str,
    head_ref: str,
    repo_root: Path,
    *,
    gated_dir: str = DEFAULT_GATED_DIR,
) -> RefVerdict | None:
    """Return a failing verdict when ``base_ref`` moved past ``head_ref``'s fork point
    IN ``gated_dir``.

    The guard exists for issue #3513: a synthetic PR merge ref cut before a
    trunk ADR fix would otherwise be scanned for duplicate ids that main has
    already renumbered. That is the only reason a stale ref matters to this
    gate, so a base that moved only OUTSIDE the ADR directory is accepted with
    a note. Measured Mon 21 Sep 2026: with main taking a merge every few
    minutes (and a `[skip ci]` TECH-DEBT regeneration after each), the bare
    ancestry check failed PR #3600's adr check twice in 30 min on refs that
    carried every ADR main had, and each failure cost a branch update and a
    full CI run on a saturated pool.

    An unnumbered ``ADR-NEW-<slug>.md`` that main ADDED is not counted as moved
    either, unless the head carries the same file name: it cannot have been
    renumbered, so it cannot leave stale duplicate ids behind. Measured Thu 15
    Oct 2026: three direct merges in 20 min each added one ADR-NEW file and
    failed the adr check of every open PR, which then needed a branch update
    and a full CI run.
    """
    try:
        base_sha = _git_rev_parse(base_ref, repo_root)
        head_sha = _git_rev_parse(head_ref, repo_root)
    except Exception as exc:
        return RefVerdict(2, f"[adr-check] UNKNOWN: could not resolve refs ({exc})")

    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_sha, head_sha],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return RefVerdict(
            2,
            "[adr-check] UNKNOWN: ancestry probe failed "
            f"(git merge-base --is-ancestor {base_ref} {head_ref}: {exc})",
        )
    if proc.returncode == 0:
        return None
    if proc.returncode == 1:
        try:
            fork = _git_lines(["merge-base", base_sha, head_sha], repo_root)
            moved = _git_lines(
                ["diff", "--name-only", fork[0], base_sha, "--", gated_dir], repo_root
            )
            added = set(
                _git_lines(
                    ["diff", "--name-only", "--diff-filter=A", fork[0], base_sha, "--", gated_dir],
                    repo_root,
                )
            )
            modified = set(
                _git_lines(
                    ["diff", "--name-only", "--diff-filter=M", fork[0], base_sha, "--", gated_dir],
                    repo_root,
                )
            )
            head_names = {
                Path(p).name
                for p in _git_lines(["ls-tree", "--name-only", head_sha, f"{gated_dir}/"], repo_root)
            }
        except Exception as exc:
            return RefVerdict(
                2, f"[adr-check] UNKNOWN: could not measure what moved on {base_ref} ({exc})"
            )
        unnumbered = [
            p for p in moved if _is_unnumbered_placeholder(p, added, modified, head_names)
        ]
        moved = [p for p in moved if p not in unnumbered]
        if not moved:
            only_new = (
                f" (only unnumbered ADR-NEW placeholders: {', '.join(unnumbered[:5])})"
                if unnumbered
                else ""
            )
            print(
                f"[adr-check] {FRESH_ENOUGH_MSG} {gated_dir}/{only_new}: "
                f"{base_ref}={base_sha[:12]} vs fork {fork[0][:12]}; accepting {head_ref}",
            )
            return None
        return RefVerdict(
            1,
            "[adr-check] "
            f"{STALE_MERGE_REF_MSG} "
            f"(base {base_ref}={base_sha[:12]} is not an ancestor of "
            f"{head_ref}={head_sha[:12]} and {gated_dir}/ moved on it: "
            f"{', '.join(moved[:5])}{', ...' if len(moved) > 5 else ''})",
        )
    return RefVerdict(
        2,
        "[adr-check] UNKNOWN: ancestry probe failed "
        f"(git merge-base --is-ancestor {base_ref} {head_ref} "
        f"rc={proc.returncode}: {proc.stderr.strip()})",
    )
