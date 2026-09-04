"""dev_loop_preflight across a history rewrite.

The shipped app is stamped with an OLD-history sha; the loop sits on the rewritten one.
Ancestry cannot answer 'is Chrome behind the app', so the commit-map must.

    [if] no --commit-map and no shared history [then] exit 4, and the line says so
    [if] the shipped tail is tooling-only and its base is in the loop [then] exit 0
    [if] the shipped tail touches apps/** [then] exit 2
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import dev_loop_preflight


def _git(cwd: Path, *args: str) -> str:
    stamp = "2026-09-03T10:00:00Z"
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    run = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    )
    return run.stdout.strip()


def _commit(cwd: Path, rel: str, msg: str) -> str:
    (cwd / rel).parent.mkdir(parents=True, exist_ok=True)
    (cwd / rel).write_text(msg)
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", msg)
    return _git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def rewritten_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """OLD: base -> ship (tooling). NEW (orphan): base' -> fix. Map base->base'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    old_base = _commit(repo, "apps/a.py", "base")
    _git(repo, "checkout", "-q", "-b", "ship-lane")
    ship_tooling = _commit(repo, ".agents/skills/ship-dmg/x.md", "ship tooling")
    _git(repo, "checkout", "-q", "main")
    ship_runtime = _commit(repo, "apps/hotfix.py", "ship runtime")  # a second 'shipped' candidate
    _git(repo, "checkout", "-q", "--orphan", "rewritten")
    _git(repo, "rm", "-rfq", ".")
    new_base = _commit(repo, "apps/a.py", "base rewritten")
    _commit(repo, "apps/fix.py", "fix on new main")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (tmp_path / "map").write_text(f"old new\n{old_base} {new_base}\n")
    app = tmp_path / "Open DJ.app"
    manifest = app / dev_loop_preflight.MANIFEST_REL
    manifest.parent.mkdir(parents=True)
    monkeypatch.chdir(repo)

    def stamp(sha: str) -> None:
        manifest.write_text(json.dumps({"identity": {"git_sha_full": sha}}))

    return app, tmp_path / "map", stamp, ship_tooling, ship_runtime


def test_no_map_and_no_shared_history_exits_4(rewritten_repo, capsys):
    app, _map, stamp, ship_tooling, _ = rewritten_repo
    stamp(ship_tooling)
    assert dev_loop_preflight.main(["--app", str(app), "--no-fetch"]) == 4, \
        "if a rewritten-history shipped sha is judged without a map then the loop guesses - broken"
    assert "--commit-map" in capsys.readouterr().out


def test_tooling_only_tail_over_a_mapped_base_passes(rewritten_repo, capsys):
    app, cmap, stamp, ship_tooling, _ = rewritten_repo
    stamp(ship_tooling)
    argv = ["--app", str(app), "--no-fetch", "--commit-map", str(cmap)]
    assert dev_loop_preflight.main(argv) == 0
    assert "tooling-only tail (1 commit(s))" in capsys.readouterr().out


def test_runtime_tail_is_refused(rewritten_repo, capsys):
    app, cmap, stamp, _, ship_runtime = rewritten_repo
    stamp(ship_runtime)
    argv = ["--app", str(app), "--no-fetch", "--commit-map", str(cmap)]
    assert dev_loop_preflight.main(argv) == 2, (
        "if the shipped app carries apps/** the loop lacks and the preflight passes "
        "then Chrome is behind - broken"
    )
    assert "apps/hotfix.py" in capsys.readouterr().out
