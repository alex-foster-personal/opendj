"""Materialize missing git objects in partial/promisor clones before file:// scans.

Supersedes: scripts/security/lib.sh sec_repo_is_partial, sec_with_origin_auth,
and the shell body of sec_materialize_missing_objects.
"""

from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

# Token order matches scripts/quality_gate.py _GIT_AUTH_ENV_KEYS (GH_TOKEN first).
_GIT_AUTH_ENV_KEYS: tuple[str, ...] = ("GH_TOKEN", "GITHUB_TOKEN")

# rev-list --objects emits "<oid> <path>" for trees/blobs; without %(rest) the whole line is
# treated as a single object name and every present blob is falsely reported missing.
_BATCH_CHECK_FORMAT = "%(objectname) %(objecttype) %(objectsize) %(rest)"


def _sec_log(message: str) -> None:
    print(f"[security] {message}", file=sys.stderr)


def _git_basic_credential(token: str) -> str:
    """base64(\"x-access-token:<token>\"); matches quality_gate._git_basic_credential."""
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _origin_auth_env(git: str, repo: Path) -> dict[str, str]:
    """Env for ONE git walk against http(s) origin; matches quality_gate._origin_auth_env."""
    token = next((os.environ[k] for k in _GIT_AUTH_ENV_KEYS if os.environ.get(k)), "")
    if not token:
        return {}
    code, url = _run_git_config_remote(git, repo)
    if code != 0 or not url.strip():
        return {}
    parsed = urlsplit(url.strip())
    if parsed.scheme not in ("http", "https"):
        return {}
    prefix = f"{parsed.scheme}://{parsed.netloc}/"
    basic = _git_basic_credential(token)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::add-mask::{basic}")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{prefix}.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


def _run_git_config_remote(git: str, repo: Path) -> tuple[int, str]:
    result = subprocess.run(
        [git, "-C", str(repo), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout


def _git_config_get(git: str, repo: Path, key: str) -> str:
    result = subprocess.run(
        [git, "-C", str(repo), "config", "--get", key],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _repo_is_partial(git: str, repo: Path) -> bool:
    if _git_config_get(git, repo, "remote.origin.promisor") == "true":
        return True
    if _git_config_get(git, repo, "remote.origin.partialclonefilter"):
        return True
    result = subprocess.run(
        [
            git,
            "-C",
            str(repo),
            "rev-list",
            "--objects",
            "--all",
            "--missing=print",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return any(line.startswith("?") for line in result.stdout.splitlines())


def _run_rev_list_batch_check(
    git: str,
    repo: Path,
    env: dict[str, str],
    *,
    capture_stdout: bool,
) -> tuple[int, int, str]:
    """Pipe rev-list --objects --all into cat-file --batch-check with %(rest) format.

    Returns (rev_list_returncode, cat_file_returncode, stdout_text_or_empty).
    """
    rev = subprocess.Popen(
        [git, "-C", str(repo), "rev-list", "--objects", "--all"],
        stdout=subprocess.PIPE,
        env=env,
    )
    assert rev.stdout is not None
    batch_argv = [git, "-C", str(repo), "cat-file", f"--batch-check={_BATCH_CHECK_FORMAT}"]
    try:
        if capture_stdout:
            batch = subprocess.run(
                batch_argv,
                stdin=rev.stdout,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            out = batch.stdout or ""
        else:
            batch = subprocess.run(
                batch_argv,
                stdin=rev.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                check=False,
            )
            out = ""
    finally:
        rev.stdout.close()
    rev_rc = rev.wait()
    return rev_rc, batch.returncode, out


def _batch_check_has_missing(git: str, repo: Path, auth_env: dict[str, str] | None = None) -> bool:
    env = os.environ.copy()
    if auth_env:
        env.update(auth_env)
    rev_rc, batch_rc, stdout = _run_rev_list_batch_check(
        git, repo, env, capture_stdout=True
    )
    if rev_rc != 0 or batch_rc != 0:
        return True
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "missing":
            return True
    return False


def _walk_missing_objects(git: str, repo: Path, auth_env: dict[str, str]) -> None:
    env = os.environ.copy()
    env.update(auth_env)
    _run_rev_list_batch_check(git, repo, env, capture_stdout=False)


def materialize_repo(git: str, repo: Path) -> int:
    if not _repo_is_partial(git, repo):
        return 0

    _sec_log(f"materializing missing git objects in {repo} (partial/promisor clone)")
    auth_env = _origin_auth_env(git, repo)
    _walk_missing_objects(git, repo, auth_env)

    if _batch_check_has_missing(git, repo, auth_env):
        url = ""
        code, url_out = _run_git_config_remote(git, repo)
        if code == 0:
            url = url_out.strip()
        token = next((os.environ[k] for k in _GIT_AUTH_ENV_KEYS if os.environ.get(k)), "")
        if url.startswith(("http://", "https://")) and not token:
            _sec_log(
                "materialize failed: https origin needs GH_TOKEN or GITHUB_TOKEN "
                "(persist-credentials: false)"
            )
        else:
            _sec_log("materialize failed: objects still missing after fetch")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Materialize missing blobs in a partial git clone."
    )
    parser.add_argument("repo", type=Path, help="Path to the git repository")
    args = parser.parse_args(argv)

    found = shutil.which("git")
    if found is None:
        print(
            "[security] git is not on PATH; cannot materialize missing objects",
            file=sys.stderr,
        )
        return 2

    return materialize_repo(found, args.repo.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
