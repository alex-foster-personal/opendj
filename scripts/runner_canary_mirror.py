"""Push one `main` SHA to `refs/heads/canary/<sha>` on the runner-canary mirror.

The mirror repository's `push` trigger on `canary/**` then runs
`.github/workflows/runner-canary.yml` there. Decision record:
docs/decisions/ADR-NEW-runner-canary.md. Config: ci/runner-canary.json.

Usage (from a checkout that has fetched main):
    git fetch origin main
    CANARY_MIRROR_PUSH_TOKEN=... python3 scripts/runner_canary_mirror.py <40-hex-sha>

Environment:
    CANARY_MIRROR_PUSH_TOKEN  required. A token with contents write on the mirror ONLY.
                              Read from the environment, handed to git through
                              GIT_CONFIG_* variables, never argv, never printed.
    CANARY_MIRROR_REPO        optional override of the config's `mirror_repository`.
                              Must still be owned by the config's `mirror_owner`.

Standard library only, so a timer host can run it with a bare python3.

Requirements (mini-PRD)
- [if] the token is missing or empty [then] exit 1 naming CANARY_MIRROR_PUSH_TOKEN and push
  nothing, [else stop] ✔︎ ✅ 🎯
- [if] the target is outside the mirror owner, is the source repository, or is not
  `owner/name` [then] exit 1 and push nothing, [else stop] ✔︎ ✅ 🎯
- [if] the SHA is malformed or not an ancestor of refs/remotes/origin/main [then] exit 1,
  [else stop] ✔︎ ✅ 🎯
- [if] the mirror has no default branch, or its default is a `canary/` branch [then] exit 1:
  the first branch pushed to an empty repository becomes its default, and the default
  branch is where every `schedule` and `workflow_run` workflow runs, [else stop] ✔︎ ✅ 🎯
- [if] all checks pass [then] exactly `refs/heads/canary/<sha>` is pushed at `<sha>`, and a
  repeat push of the same SHA is a no-op exit 0, [else stop] ✔︎ ✅ 🎯
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

TOKEN_ENV = "CANARY_MIRROR_PUSH_TOKEN"
REPO_ENV = "CANARY_MIRROR_REPO"
MAIN_REF = "refs/remotes/origin/main"
CANARY_PREFIX = "canary/"
SHA_RE = re.compile(r"[0-9a-f]{40}")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
GIT_TIMEOUT_S = 120


class MirrorRefused(Exception):
    """A named refusal. Exit 1, nothing pushed."""


# ----- decisions (pure) -------------------------------------------------------------


def resolve_mirror(config: dict, env: dict[str, str]) -> tuple[str, str]:
    """The mirror `owner/name` and where it came from, refusing anything off the owner."""
    override = env.get(REPO_ENV)
    repo, source = (override, REPO_ENV) if override else (config["mirror_repository"], "config")
    if not REPO_RE.fullmatch(repo):
        raise MirrorRefused(f"mirror {repo!r} (from {source}) is not an owner/name slug")
    if repo.lower() == config["source_repository"].lower():
        raise MirrorRefused(
            f"mirror {repo!r} is the source repository; refusing to push canary branches there"
        )
    if repo.split("/")[0] != config["mirror_owner"]:
        raise MirrorRefused(
            f"mirror {repo!r} (from {source}) is not owned by the canary owner "
            f"{config['mirror_owner']!r}"
        )
    return repo, source


def require_token(env: dict[str, str]) -> str:
    token = env.get(TOKEN_ENV, "")
    if not token.strip():
        raise MirrorRefused(
            f"{TOKEN_ENV} is missing or empty; "
            "export a token with contents write on the mirror only"
        )
    return token


def validate_sha(sha: str) -> str:
    if not SHA_RE.fullmatch(sha):
        raise MirrorRefused(f"{sha!r} is not a full 40-character lowercase commit SHA")
    return sha


def default_branch_from_ls_remote(listing: str) -> str | None:
    """The mirror's default branch, only when HEAD points at a branch that exists."""
    target = None
    heads = set()
    for line in listing.splitlines():
        if line.startswith("ref: ") and line.endswith("\tHEAD"):
            target = line[len("ref: ") : -len("\tHEAD")]
        elif "\t" in line:
            heads.add(line.split("\t", 1)[1])
    if target is None or target not in heads or not target.startswith("refs/heads/"):
        return None
    return target[len("refs/heads/") :]


def check_default_branch(branch: str | None, repo: str) -> None:
    if branch is None:
        raise MirrorRefused(
            f"{repo} has no default branch yet. The first branch pushed to an empty repository "
            "becomes its default, and the default branch runs every schedule and workflow_run "
            "workflow; bootstrap a workflow-free default branch first "
            "(docs/decisions/ADR-NEW-runner-canary.md, 'Mirror bootstrap')"
        )
    if branch.startswith(CANARY_PREFIX):
        raise MirrorRefused(
            f"{repo}'s default branch is {branch!r}, a canary branch, so its scheduled workflows "
            "are armed; point the default at the workflow-free bootstrap branch first"
        )


# ----- git (imperative shell) --------------------------------------------------------


def auth_env(token: str) -> dict[str, str]:
    """git config for the https auth header, carried in the environment, not argv."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(
    args: list[str], env: dict[str, str] | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=GIT_TIMEOUT_S,
        check=check,
    )


def require_on_main(sha: str) -> None:
    if _git(["rev-parse", "--verify", "--quiet", MAIN_REF], check=False).returncode != 0:
        raise MirrorRefused(
            f"{MAIN_REF} is missing in this checkout; run `git fetch origin main` first"
        )
    if _git(["cat-file", "-e", f"{sha}^{{commit}}"], check=False).returncode != 0:
        raise MirrorRefused(
            f"{sha} is not a commit in this checkout; run `git fetch origin main` first"
        )
    if _git(["merge-base", "--is-ancestor", sha, MAIN_REF], check=False).returncode != 0:
        raise MirrorRefused(f"{sha} is not on {MAIN_REF}; only main SHAs are mirrored")


def read_default_branch(repo: str, url: str, env: dict[str, str]) -> str | None:
    listing = _git(["ls-remote", "--symref", url], env=env, check=False)
    if listing.returncode != 0:
        raise MirrorRefused(
            f"cannot read {repo}: git ls-remote exit {listing.returncode}: {listing.stderr.strip()}"
        )
    return default_branch_from_ls_remote(listing.stdout)


def push_canary_ref(repo: str, url: str, sha: str, env: dict[str, str]) -> str:
    """Push `sha` to `canary/<sha>`, streaming git's own output. Returns the ref."""
    ref = f"refs/heads/{CANARY_PREFIX}{sha}"
    pushed = subprocess.run(
        ["git", "push", url, f"{sha}:{ref}"], env=env, timeout=GIT_TIMEOUT_S, check=False
    )
    if pushed.returncode != 0:
        raise MirrorRefused(f"git push to {repo} exited {pushed.returncode}")
    return ref


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sha")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text())
    try:
        token = require_token(dict(os.environ))
        repo, source = resolve_mirror(config, dict(os.environ))
        sha = validate_sha(args.sha)
        require_on_main(sha)
        url = f"https://github.com/{repo}.git"
        env = {**os.environ, **auth_env(token)}
        check_default_branch(read_default_branch(repo, url, env), repo)
        print(f"[mirror] target {repo} (from {source}); pushing {sha} to canary/{sha}")
        ref = push_canary_ref(repo, url, sha, env)
    except MirrorRefused as exc:
        print(f"[ERROR] runner canary mirror: {exc}")
        return 1
    print(f"[OK] {repo} {ref} -> {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
