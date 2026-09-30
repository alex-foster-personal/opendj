"""The world `scripts/runner_canary_mirror.py` is tested in: a source checkout and two bare
repositories (the mirror, and the source repository's "remote"), built in `tmp_path` and laid
out as `<remotes>/<owner>/<name>.git`, with a copy of ci/runner-canary.json whose
`remote_url_templates` are `file://` spellings of that layout. No url rewrite rule is
involved (the script refuses those), so the script's own URL construction, auth header,
preflight and push all run unmodified. Nothing touches GitHub.

Shared by tests/scripts/test_runner_canary_mirror.py, test_runner_canary_mirror_isolation.py
and test_runner_canary_mirror_redaction.py, which each register `make_world` as their
`world` fixture.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal, TypedDict

REPO = Path(__file__).resolve().parents[2]


SCRIPT = REPO / "scripts" / "runner_canary_mirror.py"


CONFIG = json.loads((REPO / "ci" / "runner-canary.json").read_text())


TOKEN = "canary-test-token-4f1d"


GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "canary-test",
    "GIT_AUTHOR_EMAIL": "canary-test@example.invalid",
    "GIT_COMMITTER_NAME": "canary-test",
    "GIT_COMMITTER_EMAIL": "canary-test@example.invalid",
}


# ----- fixtures: a real source repo and a real bare mirror ---------------------


class Remotes(TypedDict):
    env: dict[str, str]
    remotes: Path
    config: dict[str, Any]
    config_path: Path


class World(Remotes):
    source: Path
    mirror: Path
    real: Path
    on_main: str
    off_main: str


def git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def repo_url(world: Remotes, repo: str, spelling: int = 0) -> str:
    return world["config"]["remote_url_templates"][spelling].format(repo=repo)


def bare_repo(world: Remotes, repo: str) -> Path:
    """A writable bare repository at `repo`'s place in the remotes layout."""
    bare = world["remotes"] / f"{repo}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    git(bare.parent, "init", "-q", "--bare", str(bare), env=world["env"])
    return bare


def make_world(tmp_path: Path) -> World:
    gitconfig = tmp_path / "gitconfig"
    remotes = tmp_path / "remotes"
    config = {
        **CONFIG,
        "remote_url_templates": [f"file://{remotes}/{{repo}}.git", f"file://{remotes}/{{repo}}"],
    }
    config_path = tmp_path / "runner-canary.json"
    config_path.write_text(json.dumps(config))
    base_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
        **GIT_IDENTITY,
    }
    gitconfig.write_text("[init]\n\tdefaultBranch = main\n")
    world: Remotes = {
        "env": base_env,
        "remotes": remotes,
        "config": config,
        "config_path": config_path,
    }
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", env=base_env)
    (source / "a.txt").write_text("one\n")
    git(source, "add", "a.txt", env=base_env)
    git(source, "commit", "-q", "-m", "one", env=base_env)
    on_main = git(source, "rev-parse", "HEAD", env=base_env)
    git(source, "update-ref", "refs/remotes/origin/main", on_main, env=base_env)
    git(source, "checkout", "-q", "-b", "side", env=base_env)
    (source / "b.txt").write_text("two\n")
    git(source, "add", "b.txt", env=base_env)
    git(source, "commit", "-q", "-m", "two", env=base_env)
    off_main = git(source, "rev-parse", "HEAD", env=base_env)
    mirror = bare_repo(world, CONFIG["mirror_repository"])
    # The source repository's own remote: a bare repository whose default branch is main,
    # reached through the checkout's `origin` at its configured URL.
    real = bare_repo(world, CONFIG["source_repository"])
    git(source, "push", "-q", f"file://{real}", f"{on_main}:refs/heads/main", env=base_env)
    source_url = repo_url(world, CONFIG["source_repository"])
    git(source, "remote", "add", "origin", source_url, env=base_env)
    return {
        **world,
        "source": source,
        "mirror": mirror,
        "real": real,
        "on_main": on_main,
        "off_main": off_main,
    }


def bootstrap_default_branch(world: World, branch: str = "main", bare: Path | None = None) -> None:
    """What the ADR's one-time bootstrap does: a workflow-free default branch."""
    env = world["env"]
    bare = bare or world["mirror"]
    scratch = Path(str(bare) + "-boot")
    git(scratch.parent, "init", "-q", str(scratch), env=env)
    (scratch / "README.md").write_text("canary mirror\n")
    git(scratch, "add", "README.md", env=env)
    git(scratch, "commit", "-q", "-m", "boot", env=env)
    git(scratch, "push", "-q", f"file://{bare}", f"HEAD:refs/heads/{branch}", env=env)
    git(bare, "symbolic-ref", "HEAD", f"refs/heads/{branch}", env=env)


def run_mirror_script(
    world: World, sha: str, target: str | None = "mirror", **extra_env: str
) -> subprocess.CompletedProcess[str]:
    merged = {**world["env"], "CANARY_MIRROR_PUSH_TOKEN": TOKEN, **extra_env}
    env = {k: v for k, v in merged.items() if v != "<unset>"}
    target_args = [] if target is None else ["--target", target]
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            sha,
            *target_args,
            "--config",
            str(world["config_path"]),
        ],
        cwd=world["source"],
        capture_output=True,
        check=False,
        text=True,
        env=env,
        timeout=60,
    )


def canary_refs(bare: Path, world: World) -> list[str]:
    out = git(bare, "for-each-ref", "--format=%(refname)", "refs/heads/canary/", env=world["env"])
    return out.splitlines()


def refs_in(world: World, which: Literal["mirror", "real"] = "mirror") -> dict[str, str]:
    out = git(
        world[which],
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        env=world["env"],
    )
    return dict(line.split(" ", 1) for line in out.splitlines() if line)


def impostor_repo(world: World) -> tuple[str, Path]:
    """A writable bare repository at another repository's URL: a push there WOULD land."""
    other = "someone-else/music-dj-tools"
    return repo_url(world, other), bare_repo(world, other)


ENCODED = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
