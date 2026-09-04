"""The Chrome dev-loop guard must reject stale runtime inputs and broken apps.

    [if] the shipped manifest commit is in the loop [then] exit 0
    [if] a shipped-only .python-version change is missing from the loop [then] exit 2
    [if] origin/main is ahead of the loop [then] exit 3
    [if] an installed app lacks payload/manifest.json [then] the guard refuses explicitly
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import dev_loop_preflight


def _git(cwd: Path, *args: str) -> str:
    stamp = "2026-09-04T12:00:00Z"
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    )
    return result.stdout.strip()


def _commit(cwd: Path, relative_path: str, contents: str) -> str:
    target = cwd / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents)
    _git(cwd, "add", relative_path)
    _git(cwd, "commit", "-q", "-m", f"change {relative_path}")
    return _git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def loop_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    base = _commit(repo, "apps/engine.py", "base\n")
    _git(repo, "update-ref", "refs/remotes/origin/main", base)
    app = tmp_path / "Open DJ.app"
    manifest = app / dev_loop_preflight.MANIFEST_REL
    manifest.parent.mkdir(parents=True)
    monkeypatch.chdir(repo)
    return repo, manifest, base


def _stamp_manifest(manifest: Path, sha: str) -> None:
    manifest.write_text(json.dumps({"identity": {"git_sha_full": sha}}))


def test_shipped_ancestor_passes(loop_repo, capsys) -> None:
    _repo, manifest, base = loop_repo
    _stamp_manifest(manifest, base)

    assert dev_loop_preflight.main(["--app", str(manifest.parents[3]), "--no-fetch"]) == 0
    assert "| OK" in capsys.readouterr().out


def test_shipped_python_pin_change_is_refused(loop_repo, capsys) -> None:
    repo, manifest, base = loop_repo
    shipped = _commit(repo, ".python-version", "3.11.15\n")
    _git(repo, "reset", "--hard", base)
    _stamp_manifest(manifest, shipped)

    assert dev_loop_preflight.main(["--app", str(manifest.parents[3]), "--no-fetch"]) == 2, (
        "if a shipped-only .python-version update is missing from the loop and the guard passes "
        "then the dev loop can run a different interpreter - broken"
    )
    assert ".python-version" in capsys.readouterr().out


def test_loop_behind_main_is_refused(loop_repo, capsys) -> None:
    repo, manifest, base = loop_repo
    _stamp_manifest(manifest, base)
    main = _commit(repo, "apps/merged_fix.py", "merged\n")
    _git(repo, "update-ref", "refs/remotes/origin/main", main)
    _git(repo, "reset", "--hard", base)

    assert dev_loop_preflight.main(["--app", str(manifest.parents[3]), "--no-fetch"]) == 3, (
        "if origin/main is ahead and the guard starts the loop then merged fixes are absent "
        "- broken"
    )
    assert "BEHIND by 1" in capsys.readouterr().out


def test_existing_app_without_manifest_refuses(loop_repo) -> None:
    _repo, manifest, _base = loop_repo

    with pytest.raises(SystemExit, match=r"lacks payload/manifest\.json"):
        dev_loop_preflight.main(["--app", str(manifest.parents[3]), "--no-fetch"])
