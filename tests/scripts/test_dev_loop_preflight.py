"""The Chrome dev-loop guard must reject stale runtime inputs and broken apps.

    [if] the shipped manifest commit is in the loop [then] exit 0
    [if] no app is installed [then] exit 0 and report shipped n/a
    [if] an installed manifest has no git_sha [then] exit 1 and refuse to guess
    [if] a shipped-only .python-version change is missing from the loop [then] exit 2
    [if] origin/main is ahead of the loop [then] exit 3
    [if] origin/main is ahead and --allow-behind-main is supplied [then] exit 0
    [if] a shipped-only divergence changes tooling alone [then] exit 0
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


def test_no_installed_app_is_reported_and_skipped(loop_repo, capsys) -> None:
    _repo, manifest, _base = loop_repo

    assert (
        dev_loop_preflight.main(
            ["--app", str(manifest.parents[3] / "missing.app"), "--no-fetch"]
        )
        == 0
    )
    assert "shipped n/a (no app installed)" in capsys.readouterr().out


def test_shipped_ancestor_passes(loop_repo, capsys) -> None:
    _repo, manifest, base = loop_repo
    _stamp_manifest(manifest, base)

    assert dev_loop_preflight.main(["--app", str(manifest.parents[3]), "--no-fetch"]) == 0
    assert "| OK" in capsys.readouterr().out


def test_manifest_without_git_sha_refuses_to_guess(loop_repo) -> None:
    _repo, manifest, _base = loop_repo
    manifest.write_text(json.dumps({"identity": {}}))

    with pytest.raises(SystemExit) as exc_info:
        dev_loop_preflight.main(["--app", str(manifest.parents[3]), "--no-fetch"])

    assert exc_info.value.code == (
        f"[dev-loop] manifest at {manifest} carries no git_sha: refusing to guess"
    )


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


def test_allow_behind_main_overrides_main_refusal(loop_repo, capsys) -> None:
    repo, manifest, base = loop_repo
    _stamp_manifest(manifest, base)
    main = _commit(repo, "apps/merged_fix.py", "merged\n")
    _git(repo, "update-ref", "refs/remotes/origin/main", main)
    _git(repo, "reset", "--hard", base)

    assert (
        dev_loop_preflight.main(
            ["--app", str(manifest.parents[3]), "--no-fetch", "--allow-behind-main"]
        )
        == 0
    )
    assert "main " in capsys.readouterr().out


def test_shipped_tooling_only_divergence_passes(loop_repo, capsys) -> None:
    repo, manifest, base = loop_repo
    shipped = _commit(repo, "scripts/ship_note.py", "tooling only\n")
    _git(repo, "reset", "--hard", base)
    _stamp_manifest(manifest, shipped)

    assert dev_loop_preflight.main(["--app", str(manifest.parents[3]), "--no-fetch"]) == 0
    assert "diverged, tooling-only (1 commit(s), no runtime paths)" in capsys.readouterr().out


def test_existing_app_without_manifest_refuses(loop_repo) -> None:
    _repo, manifest, _base = loop_repo

    with pytest.raises(SystemExit, match=r"lacks payload/manifest\.json"):
        dev_loop_preflight.main(["--app", str(manifest.parents[3]), "--no-fetch"])
