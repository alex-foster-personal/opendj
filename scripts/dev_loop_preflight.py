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
    return subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant]).returncode == 0


def _count(rev_range: str) -> int:
    return int(_git("rev-list", "--count", rev_range))


RUNTIME_PREFIXES = ("apps/", "pyproject.toml", "uv.lock")


def _runtime_paths_missing(head: str, shipped: str) -> list[str]:
    """Files changed by shipped-only commits that alter what the app runs (apps/**, deps)."""
    changed = _git("diff", "--name-only", f"{head}...{shipped}").splitlines()
    return [p for p in changed if p.startswith(RUNTIME_PREFIXES) and not p.endswith(".md")]


def _shipped_sha(app: Path) -> str | None:
    manifest = app / MANIFEST_REL
    if not manifest.exists():
        return None
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
    else:
        # The shipped commit is not in this loop. That only matters if the commits the
        # loop lacks change what the app DOES: a ship-script or docs-only lane (e.g. the
        # 4158f361 build of Wed 2 Sep 2026, one file under .agents/skills/ship-dmg) runs
        # the same app code as main.
        missing_runtime = _runtime_paths_missing(head, shipped)
        if missing_runtime:
            parts.append(f"shipped {shipped[:8]} BEHIND: {len(missing_runtime)} runtime file(s) e.g. {missing_runtime[0]}")
            verdict, code = "REFUSED: loop lacks fixes the shipped app has", 2
        else:
            parts.append(f"shipped {shipped[:8]} diverged, tooling-only ({_count(f'{head}..{shipped}')} commit(s), no runtime paths)")

    if _is_ancestor(main_sha, head):
        parts.append(f"main {main_sha[:8]} +{_count(f'{main_sha}..{head}')}")
    else:
        behind_main = _count(f"{head}..{main_sha}")
        parts.append(f"main {main_sha[:8]} BEHIND by {behind_main}")
        if code == 0 and not args.allow_behind_main:
            verdict, code = "REFUSED: loop lacks merged fixes on main (rebase, or --allow-behind-main)", 3

    print(f"[dev-loop] {' | '.join(parts)} | {verdict}")
    return code


if __name__ == "__main__":
    sys.exit(main())
