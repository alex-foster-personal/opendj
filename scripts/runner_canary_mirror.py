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
                              through GIT_CONFIG_* variables, never argv, never printed,
                              and only on the mirror's network calls: it is stripped from
                              every git child's environment, on either target.
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
- [if] any git url rewrite rule (`insteadOf` or `pushInsteadOf`, any config scope) matches
  the URL about to be contacted, on either target [then] exit 1 and push nothing: the
  checked URL must be the destination, [else stop] ✔︎ ✅ 🎯
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
REWRITE_RULES_RE = r"^url\..*\.(push)?insteadof$"
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


def remote_urls_of(repo: str, config: dict) -> list[str]:
    """Every accepted spelling of `repo`'s remote URL, from `remote_url_templates`. The first
    is the one this script pushes to when it builds the URL itself (the mirror)."""
    return [template.format(repo=repo) for template in config["remote_url_templates"]]


def require_origin_is_source(origin_url: str, config: dict) -> None:
    spellings = {url.lower() for url in remote_urls_of(config["source_repository"], config)}
    if origin_url.strip().lower() not in spellings:
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


def rewrites_matching(url: str, rules: list[tuple[str, str]]) -> list[str]:
    """The `url.<base>.insteadOf` / `pushInsteadOf` rules git would apply to `url`: git
    rewrites a URL when a rule's value is a prefix of it (the longest wins), so any match
    means the address git contacts is not `url`."""
    return [f"{key} = {prefix!r}" for key, prefix in rules if url.startswith(prefix)]


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


def child_env(auth: dict[str, str]) -> dict[str, str]:
    """The environment of EVERY git child: this process's, minus the mirror token, plus
    `auth`. git never needs the raw token, only the mirror's auth header, and that header
    is `auth` only on the mirror's own network calls. So a `--target source` push, and every
    hook or credential helper its git starts, never sees the mirror credential."""
    return {**{k: v for k, v in os.environ.items() if k != TOKEN_ENV}, **auth}


def _git(
    args: list[str], auth: dict[str, str] | None = None, check: bool = True, capture: bool = True
) -> subprocess.CompletedProcess:
    """The one spawn site: every git child gets `child_env`, never the inherited env."""
    return subprocess.run(
        ["git", *args],
        capture_output=capture,
        text=True,
        env=child_env(auth or {}),
        timeout=GIT_TIMEOUT_S,
        check=check,
    )


def url_rewrite_rules() -> list[tuple[str, str]]:
    """Every url rewrite rule git sees, from every config scope (system, global, local,
    worktree, and the inherited GIT_CONFIG_* environment), read through git itself. Read
    without the mirror's auth env, which replaces the inherited GIT_CONFIG_* entries, so this
    is a superset of what the mirror's network calls see and exactly what the source's see."""
    listed = _git(["config", "-z", "--get-regexp", REWRITE_RULES_RE], check=False)
    if listed.returncode not in (0, 1):  # 1 is git's "no such key": no rules
        raise MirrorRefused(
            f"cannot read git's url rewrite rules: git config exit {listed.returncode}: "
            f"{listed.stderr.strip()}"
        )
    return [
        (key, prefix)
        for key, _, prefix in (entry.partition("\n") for entry in listed.stdout.split("\0"))
        if key
    ]


def require_no_rewrite(url: str) -> None:
    """Refuse when git would contact anything but `url` itself: the checked address must be
    the address git uses, on both targets (Codex P1 on d18006414)."""
    matching = rewrites_matching(url, url_rewrite_rules())
    if matching:
        raise MirrorRefused(
            f"git would rewrite {url!r} before contacting it ({'; '.join(matching)}), so the "
            "checked URL is not the destination; remove the rule or run from a checkout "
            "without it"
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


def read_default_branch(repo: str, url: str, auth: dict[str, str]) -> str | None:
    listing = _git(["ls-remote", "--symref", url], auth=auth, check=False)
    if listing.returncode != 0:
        raise MirrorRefused(
            f"cannot read {repo}: git ls-remote exit {listing.returncode}: {listing.stderr.strip()}"
        )
    return default_branch_from_ls_remote(listing.stdout)


def push_canary_ref(repo: str, url: str, sha: str, auth: dict[str, str]) -> str:
    """Push `sha` to `canary/<sha>`, streaming git's own output. Returns the ref."""
    ref = f"refs/heads/{CANARY_PREFIX}{sha}"
    pushed = _git(["push", url, f"{sha}:{ref}"], auth=auth, check=False, capture=False)
    if pushed.returncode != 0:
        raise MirrorRefused(f"git push to {repo} exited {pushed.returncode}")
    return ref


def _mirror_destination(config: dict) -> tuple[str, str, dict[str, str]]:
    """(repo, url, auth header env for the mirror's network calls)."""
    token = require_token(dict(os.environ))
    # The mirror's owner and its distinctness from the source are config invariants,
    # pinned by tests/scripts/test_runner_canary_budget.py.
    repo = config["mirror_repository"]
    return repo, remote_urls_of(repo, config)[0], auth_env(token)


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
    # the address that was validated. No auth header: git's own credentials, never the
    # mirror's (child_env strips the token from every child).
    return config["source_repository"], push_url, {}


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
        repo, url, auth = _destination(args.target, config)
        require_no_rewrite(url)
        sha = validate_sha(args.sha)
        require_on_main(sha)
        check_default_branch(read_default_branch(repo, url, auth), repo)
        print(f"[canary] target {args.target} = {repo}; pushing {sha} to canary/{sha}")
        ref = push_canary_ref(repo, url, sha, auth)
    except MirrorRefused as exc:
        print(f"[ERROR] runner canary mirror: {exc}")
        return 1
    print(f"[OK] {repo} {ref} -> {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
