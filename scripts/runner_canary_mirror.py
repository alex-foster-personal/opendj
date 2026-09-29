"""Push one `main` SHA to `refs/heads/canary/<sha>` in a runner-canary target repository.

That repository's `push` trigger on `canary/**` then runs
`.github/workflows/runner-canary.yml` there, for the vendors whose config `target_repo`
it is. Decision record: docs/decisions/ADR-NEW-runner-canary.md. Config:
ci/runner-canary.json.

Targets:
    --target mirror   the canary org's mirror (`mirror_repository`): Blacksmith and
                      Ubicloud, whose runners are organization-only.
    --target source   the source repository itself (`source_repository`), through this
                      checkout's own `origin` and git's own credentials: Avrea and Tenki.

Usage (from a checkout that has fetched main):
    git fetch origin main
    CANARY_MIRROR_PUSH_TOKEN=... python3 scripts/runner_canary_mirror.py <sha> \\
        --target mirror --config ci/runner-canary.json
    python3 scripts/runner_canary_mirror.py <sha> --target source --config ci/runner-canary.json

Environment:
    CANARY_MIRROR_PUSH_TOKEN  required for --target mirror. A token with contents write on
                              the mirror ONLY. Read from the environment, handed to git
                              through GIT_CONFIG_* variables, never argv, never printed.
    CANARY_MIRROR_REPO        RETIRED, and refused if set: an override could land a canary
                              branch in a repository whose budget gate starts at zero.

Standard library only, so a timer host can run it with a bare python3.

Requirements (mini-PRD)
- [if] --target mirror and the token is missing or empty [then] exit 1 naming
  CANARY_MIRROR_PUSH_TOKEN and push nothing, [else stop] ✔︎ ✅ 🎯
- [if] CANARY_MIRROR_REPO is set [then] exit 1 naming it and push nothing, [else stop]
  ✔︎ ✅ 🎯
- [if] --target source and the URL `git push origin` would use (pushurl, else url) is not
  the configured source repository, or there is more than one pushurl [then] exit 1 and
  push nothing, [else stop] ✔︎ ✅ 🎯
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
GITHUB_REMOTE_RE = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?"
)
TARGETS = ("mirror", "source")
GIT_TIMEOUT_S = 120


class MirrorRefused(Exception):
    """A named refusal. Exit 1, nothing pushed."""


# ----- decisions (pure) -------------------------------------------------------------


def refuse_retired_override(env: dict[str, str]) -> None:
    if REPO_ENV in env:
        raise MirrorRefused(
            f"{REPO_ENV} is no longer honored: a canary branch lands only in a configured "
            "target (--target mirror|source), because a repository the config does not name "
            "has a budget gate that starts at zero; unset it"
        )


def github_repo_of_remote(url: str) -> str | None:
    """`owner/name` of a GitHub remote URL in any of git's spellings, else None."""
    match = GITHUB_REMOTE_RE.fullmatch(url.strip())
    return match.group("repo") if match else None


def require_origin_is_source(origin_url: str, config: dict) -> None:
    repo = github_repo_of_remote(origin_url)
    if repo is None or repo.lower() != config["source_repository"].lower():
        raise MirrorRefused(
            f"origin is {origin_url!r}, not the source repository "
            f"{config['source_repository']!r}; run from a checkout of it"
        )


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


def _mirror_destination(config: dict) -> tuple[str, str, dict[str, str]]:
    token = require_token(dict(os.environ))
    # The mirror's owner and its distinctness from the source are config invariants,
    # pinned by tests/scripts/test_runner_canary_budget.py.
    repo = config["mirror_repository"]
    return repo, f"https://github.com/{repo}.git", {**os.environ, **auth_env(token)}


def origin_push_url() -> str:
    """The URL `git push origin` would use: `remote.origin.pushurl` when set, else `url`.

    More than one pushurl is refused: git would push to every one of them.
    """
    pushurls = _git(["config", "--get-all", "remote.origin.pushurl"], check=False).stdout.split()
    if len(pushurls) > 1:
        raise MirrorRefused(
            f"origin has {len(pushurls)} push URLs {pushurls}; git would push to all of them"
        )
    if pushurls:
        return pushurls[0]
    url = _git(["config", "--get", "remote.origin.url"], check=False)
    return url.stdout.strip() if url.returncode == 0 else ""


def _source_destination(config: dict) -> tuple[str, str, dict[str, str]]:
    push_url = origin_push_url()
    require_origin_is_source(push_url, config)
    # Push to the URL just checked, not to the name `origin`, so the address git uses is
    # the address that was validated. git's own credentials: never the mirror token.
    return config["source_repository"], push_url, dict(os.environ)


def _destination(target: str, config: dict) -> tuple[str, str, dict[str, str]]:
    if target == "mirror":
        return _mirror_destination(config)
    if target == "source":
        return _source_destination(config)
    raise MirrorRefused(f"unhandled target {target!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sha")
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text())
    try:
        refuse_retired_override(dict(os.environ))
        repo, url, env = _destination(args.target, config)
        sha = validate_sha(args.sha)
        require_on_main(sha)
        check_default_branch(read_default_branch(repo, url, env), repo)
        print(f"[canary] target {args.target} = {repo}; pushing {sha} to canary/{sha}")
        ref = push_canary_ref(repo, url, sha, env)
    except MirrorRefused as exc:
        print(f"[ERROR] runner canary mirror: {exc}")
        return 1
    print(f"[OK] {repo} {ref} -> {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
