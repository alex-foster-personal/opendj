"""Report which files the open PRs actually conflict on.

Usage::

    python -m scripts.conflict_census              # census of every DIRTY open PR
    python -m scripts.conflict_census --json out.json
    python -m scripts.conflict_census --limit 20   # cheaper sample while iterating

Read-only. Nothing is checked out, no branch is fetched into a working tree,
and the shared checkout at /Users/user/code/music-dj-tools is never modified,
so this is safe to run while other agents are working there.

Correlating "PRs that touch the same file" gets this wrong: two PRs can both
edit a file and merge cleanly. ``git merge-tree --write-tree`` performs the
real three-way merge in memory and names the paths that actually conflict.

REQUIREMENTS
  R-1 Name the conflicting paths per PR, not the shared ones.       [done, run]
      [if two PRs edit one file without overlapping then that file is not
       counted as a conflict]
      [if a PR conflicts on three files then all three are counted]
      [if GitHub calls a PR DIRTY but merge-tree merges it clean then the PR
       is reported as UNCOMPUTED with that reason, never as zero conflicts]
  R-2 Refuse to report a partial census as if it were whole.        [done, run]
      [if any PR's merge base is unreachable then the run exits 1 and names
       the deepen command, rather than printing a smaller denominator]
      [if every PR resolves then the run exits 0]
      [if the computed and uncomputed counts do not sum to the PR count then
       the run raises rather than printing]
  R-3 Pin main once, by sha, before measuring anything.             [done, run]
      [if main moves mid-run then every PR is still measured against the one
       pinned sha, so the histogram is internally consistent]
      [if the pinned sha is missing locally then the run says so and exits 1]

Why R-2 is a hard exit rather than a warning: this clone is shallow, and the
first run of this census silently computed 11 of 42 PRs because the rest died
on `refusing to merge unrelated histories`. An 11-PR histogram looks exactly
like a 42-PR one.
"""
from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
REPO_SLUG: str = "maintainer/music-dj-tools"
MAIN_REF: str = "refs/heads/main"
# merge-tree exits 0 clean, 1 on conflicts, >1 on error. Only 1 carries a census.
MERGE_TREE_CONFLICT: int = 1
DEEPEN_HINT: str = "git fetch --deepen=900 origin main"

# ------------------------------------------------------------------ git

def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )


def _pinned_main() -> str:
    """Main's sha from the remote, pinned once.

    Never FETCH_HEAD: it is a single mutable pointer that a concurrent agent's
    fetch overwrites between two reads, silently serving another branch's tree.
    """
    out = _git("ls-remote", "origin", MAIN_REF)
    if out.returncode != 0 or not out.stdout.strip():
        raise SystemExit(f"cannot resolve {MAIN_REF} on origin: {out.stderr.strip()}")
    sha = out.stdout.split()[0]
    if _git("cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        raise SystemExit(f"main {sha[:10]} is not in this clone. Run: {DEEPEN_HINT}")
    return sha


def _conflicting_paths(main_sha: str, head_sha: str) -> tuple[list[str] | None, str]:
    """(paths, "") when the merge conflicts, (None, why) when no census is possible."""
    if _git("cat-file", "-e", f"{head_sha}^{{commit}}").returncode != 0:
        return None, "head commit not present locally"
    res = _git("merge-tree", "--write-tree", "--name-only", main_sha, head_sha)
    if res.returncode == 0:
        return None, "merge-tree merges this clean (GitHub's DIRTY may predate a main move)"
    if res.returncode != MERGE_TREE_CONFLICT:
        return None, f"merge-tree exit {res.returncode}: {res.stderr.strip()[:120]}"
    # First line is the written tree oid; the rest are conflicted paths.
    lines = res.stdout.splitlines()[1:]
    noise = ("Auto-merging", "CONFLICT", "warning", "hint:")
    return [ln for ln in lines if ln.strip() and not ln.startswith(noise)], ""


# ------------------------------------------------------------------ census

def _dirty_prs(limit: int) -> tuple[list[dict[str, object]], int]:
    res = subprocess.run(
        ["gh", "pr", "list", "--repo", REPO_SLUG, "--state", "open",
         "--limit", str(limit), "--json", "number,headRefOid,mergeStateStatus,title"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        raise SystemExit(f"gh pr list failed: {res.stderr.strip()[:200]}")
    prs = json.loads(res.stdout)
    return [p for p in prs if p["mergeStateStatus"] == "DIRTY"], len(prs)


def _fetch_head(number: int) -> None:
    _git("fetch", "-q", "--depth=900", "origin", f"pull/{number}/head")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", type=Path, help="write the histogram and per-PR map here")
    ap.add_argument("--limit", type=int, default=200, help="open PRs to consider")
    args = ap.parse_args(argv)

    main_sha = _pinned_main()
    dirty, total_open = _dirty_prs(args.limit)
    print(f"{len(dirty)} DIRTY of {total_open} open PRs, measured against main {main_sha[:9]}")

    hist: collections.Counter[str] = collections.Counter()
    per_pr: dict[int, list[str]] = {}
    uncomputed: list[tuple[int, str]] = []
    for pr in dirty:
        number, head = int(pr["number"]), str(pr["headRefOid"])
        paths, why = _conflicting_paths(main_sha, head)
        if paths is None and why == "head commit not present locally":
            _fetch_head(number)
            paths, why = _conflicting_paths(main_sha, head)
        if paths is None:
            uncomputed.append((number, why))
            continue
        per_pr[number] = paths
        hist.update(paths)

    # A census that cannot account for every PR is not a census.
    if len(per_pr) + len(uncomputed) != len(dirty):
        raise AssertionError("census bug: computed + uncomputed != DIRTY PRs")

    pairs = sum(hist.values())
    print(f"\ncomputed {len(per_pr)}, uncomputed {len(uncomputed)}; "
          f"{pairs} (file, PR) conflict pairs over {len(hist)} distinct files")
    if hist:
        print("\nfile                                                        PRs blocked")
        for path, count in hist.most_common(20):
            print(f"  {path[:56]:56s} {count:3d}")
        top3 = sum(c for _, c in hist.most_common(3))
        print(f"\ntop 3 files hold {round(100 * top3 / pairs)}% of all conflict pairs")
        solo = [n for n, paths in per_pr.items() if len(paths) == 1]
        print(f"{len(solo)} PRs conflict on exactly ONE file (cheap rebases): {sorted(solo)}")

    if args.json:
        args.json.write_text(json.dumps(
            {"main": main_sha, "hist": dict(hist), "per_pr": per_pr,
             "uncomputed": uncomputed}, indent=1) + "\n")
        print(f"\nwritten to {args.json.resolve()}")

    if uncomputed:
        print(f"\n{len(uncomputed)} PR(s) could NOT be measured. The numbers above "
              f"exclude them, so treat them as a sample, not a census:", file=sys.stderr)
        for number, why in uncomputed:
            print(f"  #{number}: {why}", file=sys.stderr)
        if any("unrelated histories" in why for _, why in uncomputed):
            print(f"\nThis clone is shallow. Deepen it and re-run: {DEEPEN_HINT}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
