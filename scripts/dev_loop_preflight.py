"""Refuse to start a dev loop whose code is BEHIND the shipped app.

    python -m scripts.dev_loop_preflight [--app "/Applications/Open DJ.app"] [--allow-behind-main]

The Chrome dev loop (docs/architecture/chrome-dev-loop.md) serves whatever commit the
worktree sits on. The shipped DMG is stamped with its own commit in
``Contents/Resources/payload/manifest.json``. Nothing else compares the two, so a reviewer
can look at Chrome and miss fixes the WebKit build already has. the maintainer's rule (Wed 2 Sep
2026): Chrome AHEAD of the app is fine; Chrome BEHIND it is unacceptable.

Checks, in order:

1. shipped build SHA (from the installed app's manifest) is an ancestor of HEAD -> else exit 2
2. origin/main is an ancestor of HEAD (fetched fresh) -> else exit 3, unless --allow-behind-main,
   because merged fixes from agents land on main and a review loop should carry them too
3. prints one line the launch log can show:
   ``[dev-loop] HEAD 6341f421 (af--docs-x) | shipped 4158f361 +23 | main 525e16a9 +2 | OK``

No app installed -> check 1 is reported as ``shipped n/a`` and skipped, never assumed.

History rewrite (Thu 3 Sep 2026, #911): the shipped app is stamped with a SHA from the OLD
history, which shares no merge base with the rewritten one, so check 1 cannot be answered
by ancestry alone. Pass ``--commit-map`` (filter-repo's ``old new`` pairs, published under
``odj-private/rewrite/``): the shipped commit's first-parent chain is walked to the first
mapped ancestor, that ancestor's NEW sha must be in HEAD, and the unmapped tail (the
build's own ship-lane commits) is diffed for runtime paths exactly as before. No map and
no shared history -> exit 4, never a guess.

    [if] shipped sha shares no history with HEAD and no --commit-map [then] exit 4 with the map path
    [if] the shipped commit's first mapped ancestor is not in HEAD [then] exit 2 (loop is behind)
    [if] the unmapped tail touches apps/** [then] exit 2; tooling-only tail [then] OK
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

DEFAULT_APP = Path("/Applications/Open DJ.app")
MANIFEST_REL = Path("Contents/Resources/payload/manifest.json")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant], check=False
    )
    return result.returncode == 0


def _count(rev_range: str) -> int:
    return int(_git("rev-list", "--count", rev_range))


def _shares_history(a: str, b: str) -> bool:
    probe = subprocess.run(["git", "merge-base", a, b], capture_output=True, check=False)
    return probe.returncode == 0


def _load_commit_map(path: Path) -> dict[str, str]:
    pairs = (line.split() for line in path.read_text().splitlines())
    mapping = {old: new for old, new in pairs if old != "old"}
    if not mapping:
        raise SystemExit(f"[dev-loop] commit-map at {path} holds no old/new pairs")
    return mapping


def _first_mapped_ancestor(
    shipped: str, commit_map: dict[str, str]
) -> tuple[str, str, int]:
    """(old_base, new_base, unmapped_tail_length) walking shipped's first-parent chain."""
    chain = _git("rev-list", "--first-parent", shipped).splitlines()
    for depth, old in enumerate(chain):
        if old in commit_map:
            return old, commit_map[old], depth
    raise SystemExit(
        f"[dev-loop] no commit on {shipped[:8]}'s first-parent chain is in the commit-map"
    )


RUNTIME_PREFIXES = ("apps/", "pyproject.toml", "uv.lock", ".python-version")


def _runtime_paths_missing(head: str, shipped: str) -> list[str]:
    """Files changed by shipped-only commits that alter the app runtime."""
    changed = _git("diff", "--name-only", f"{head}...{shipped}").splitlines()
    return [p for p in changed if p.startswith(RUNTIME_PREFIXES) and not p.endswith(".md")]


def _shipped_sha(app: Path) -> str | None:
    if not app.exists():
        return None
    manifest = app / MANIFEST_REL
    if not manifest.is_file():
        raise SystemExit(
            f"[dev-loop] installed app at {app} lacks payload/manifest.json: refusing to guess"
        )
    data = json.loads(manifest.read_text())
    identity = data.get("identity", data)
    sha = identity.get("git_sha_full") or identity.get("git_sha")
    if not sha:
        raise SystemExit(f"[dev-loop] manifest at {manifest} carries no git_sha: refusing to guess")
    return sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app", type=Path, default=DEFAULT_APP)
    parser.add_argument("--allow-behind-main", action="store_true")
    parser.add_argument("--no-fetch", action="store_true", help="skip `git fetch origin main`")
    parser.add_argument(
        "--commit-map", type=Path, default=None,
        help="filter-repo 'old new' pairs, needed only while the shipped app predates a rewrite",
    )
    args = parser.parse_args(argv)

    head = _git("rev-parse", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if not args.no_fetch:
        subprocess.run(["git", "fetch", "-q", "origin", "main"], check=True)
    main_sha = _git("rev-parse", "origin/main")

    parts = [f"HEAD {head[:8]} ({branch})"]
    verdict = "OK"
    code = 0

    shipped = _shipped_sha(args.app)
    if shipped is None:
        parts.append("shipped n/a (no app installed)")
    elif _is_ancestor(shipped, head):
        parts.append(f"shipped {shipped[:8]} +{_count(f'{shipped}..{head}')}")
    elif not _shares_history(shipped, head):
        if args.commit_map is None:
            parts.append(f"shipped {shipped[:8]} is from a rewritten history")
            verdict = "REFUSED: pass --commit-map (odj-private/rewrite/) or rebuild the app"
            code = 4
        else:
            old_base, new_base, tail = _first_mapped_ancestor(
                shipped, _load_commit_map(args.commit_map)
            )
            missing_runtime = [
                p for p in _git("diff", "--name-only", old_base, shipped).splitlines()
                if p.startswith(RUNTIME_PREFIXES) and not p.endswith(".md")
            ]
            if not _is_ancestor(new_base, head):
                parts.append(f"shipped {shipped[:8]} base {new_base[:8]} (rewritten) not in loop")
                verdict, code = "REFUSED: loop lacks the shipped app's base commit", 2
            elif missing_runtime:
                parts.append(
                    f"shipped {shipped[:8]} BEHIND: {len(missing_runtime)} runtime file(s) "
                    f"e.g. {missing_runtime[0]}"
                )
                verdict, code = "REFUSED: loop lacks fixes the shipped app has", 2
            else:
                parts.append(
                    f"shipped {shipped[:8]} rewritten, base {new_base[:8]} in loop, "
                    f"tooling-only tail ({tail} commit(s))"
                )
    else:
        # The shipped commit is not in this loop. That only matters if the commits the
        # loop lacks change what the app DOES: a ship-script or docs-only lane (e.g. the
        # 4158f361 build of Wed 2 Sep 2026, one file under .agents/skills/ship-dmg) runs
        # the same app code as main.
        missing_runtime = _runtime_paths_missing(head, shipped)
        if missing_runtime:
            parts.append(
                f"shipped {shipped[:8]} BEHIND: {len(missing_runtime)} runtime file(s) "
                f"e.g. {missing_runtime[0]}"
            )
            verdict, code = "REFUSED: loop lacks fixes the shipped app has", 2
        else:
            parts.append(
                f"shipped {shipped[:8]} diverged, tooling-only "
                f"({_count(f'{head}..{shipped}')} commit(s), no runtime paths)"
            )

    if _is_ancestor(main_sha, head):
        parts.append(f"main {main_sha[:8]} +{_count(f'{main_sha}..{head}')}")
    else:
        behind_main = _count(f"{head}..{main_sha}")
        parts.append(f"main {main_sha[:8]} BEHIND by {behind_main}")
        if code == 0 and not args.allow_behind_main:
            verdict, code = (
                "REFUSED: loop lacks merged fixes on main (rebase, or --allow-behind-main)",
                3,
            )

    print(f"[dev-loop] {' | '.join(parts)} | {verdict}")
    return code


if __name__ == "__main__":
    sys.exit(main())
