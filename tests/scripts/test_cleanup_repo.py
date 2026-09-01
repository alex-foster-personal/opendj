"""Real-filesystem acceptance tests for :mod:`scripts.cleanup_repo`."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.cleanup_repo import CLEANUP_TARGETS

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/cleanup_repo.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        capture_output=True,
        text=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "cleanup@example.test")
    _git(repo, "config", "user.name", "Cleanup Test")
    (repo / "seed.txt").write_text("tracked\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-qm", "seed")
    return repo


def _run(
    repo: Path,
    trash: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--trash-root",
            str(trash),
            *args,
        ),
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _artifact(repo: Path, relative: str, body: bytes = b"generated") -> Path:
    artifact = repo / relative / "artifact.bin"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(body)
    return artifact


def test_default_is_a_non_mutating_dry_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trash = tmp_path / "trash"
    artifact = _artifact(repo, "spikes/rusty-link/target")

    result = _run(repo, trash)

    assert result.returncode == 0, result.stderr
    assert "[DRY RUN] nothing moved" in result.stdout
    assert artifact.is_file()
    assert not trash.exists(), "a dry run must not even create a Trash directory"


def test_apply_moves_only_the_allowlist_and_writes_a_recovery_manifest(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    trash = tmp_path / "trash"
    for target in CLEANUP_TARGETS:
        _artifact(repo, target.relative_path, target.relative_path.encode())

    authored = repo / "spikes/rusty-link/README.md"
    authored.write_text("authored spike note\n")
    evidence = repo / "spikes/carabiner-link/evidence/probe.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{"observed": true}\n')
    unknown_cache = _artifact(repo, "spikes/another-spike/target")

    result = _run(repo, trash, "--apply")

    assert result.returncode == 0, result.stderr
    assert authored.read_text() == "authored spike note\n"
    assert evidence.read_text() == '{"observed": true}\n'
    assert unknown_cache.is_file(), "generic cache names outside the allowlist stay visible"
    for target in CLEANUP_TARGETS:
        assert not (repo / target.relative_path).exists()

    sessions = [path for path in trash.iterdir() if path.is_dir()]
    assert len(sessions) == 1
    manifest = json.loads((sessions[0] / "cleanup-manifest.json").read_text())
    assert manifest["state"] == "complete"
    assert {move["relative_path"] for move in manifest["moves"]} == {
        target.relative_path for target in CLEANUP_TARGETS
    }
    assert all(move["status"] == "moved" for move in manifest["moves"])
    for target in CLEANUP_TARGETS:
        assert (sessions[0] / target.relative_path / "artifact.bin").is_file()


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof unavailable")
def test_apply_refuses_a_real_open_file_without_moving_anything(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trash = tmp_path / "trash"
    artifact = _artifact(repo, "spikes/rusty-link/target")

    with artifact.open("rb"):
        result = _run(repo, trash, "--apply")

    assert result.returncode == 3
    assert "active use" in result.stderr
    assert artifact.is_file()
    assert not trash.exists(), "the live-use gate runs before a Trash session is created"


def test_apply_refuses_a_trash_root_inside_the_repository(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    artifact = _artifact(repo, "spikes/rusty-link/target")

    result = _run(repo, repo / ".trash", "--apply")

    assert result.returncode == 3
    assert "outside the repository" in result.stderr
    assert artifact.is_file()


def test_apply_refuses_when_lsof_is_unavailable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trash = tmp_path / "trash"
    artifact = _artifact(repo, "spikes/rusty-link/target")
    git = shutil.which("git")
    assert git is not None

    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "git").symlink_to(git)
    env = os.environ.copy()
    env["PATH"] = str(tools)

    result = _run(repo, trash, "--apply", env=env)

    assert result.returncode == 3
    assert "lsof is unavailable" in result.stderr
    assert "refusing --apply" in result.stderr
    assert artifact.is_file()
    assert not trash.exists()


def test_apply_refuses_a_registered_worktree_inside_a_target(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trash = tmp_path / "trash"
    linked = repo / "spikes/rusty-link/target/active-worktree"
    linked.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "-q", "-b", "cleanup-linked", str(linked))

    result = _run(repo, trash, "--apply")

    assert result.returncode == 3
    assert "registered worktree" in result.stderr
    assert (linked / "seed.txt").is_file()
    assert not trash.exists()


def test_apply_refuses_a_foreign_linked_worktree_inside_a_target(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trash = tmp_path / "trash"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _git(foreign, "init", "-q", "-b", "main")
    _git(foreign, "config", "user.email", "cleanup@example.test")
    _git(foreign, "config", "user.name", "Cleanup Test")
    (foreign / "foreign.txt").write_text("tracked\n")
    _git(foreign, "add", "foreign.txt")
    _git(foreign, "commit", "-qm", "seed")

    linked = repo / "spikes/rusty-link/target"
    linked.parent.mkdir(parents=True)
    _git(foreign, "worktree", "add", "-q", "-b", "foreign-linked", str(linked))

    result = _run(repo, trash, "--apply")

    assert result.returncode == 3
    assert "linked Git worktree marker" in result.stderr
    assert (linked / "foreign.txt").is_file()
    assert not trash.exists()


def test_unrelated_sibling_worktree_does_not_block_cleanup(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trash = tmp_path / "trash"
    sibling = tmp_path / "sibling"
    _git(repo, "worktree", "add", "-q", "-b", "cleanup-sibling", str(sibling))
    artifact = _artifact(repo, "spikes/rusty-link/target")

    result = _run(repo, trash, "--apply")

    assert result.returncode == 0, result.stderr
    assert not artifact.exists()
    assert (sibling / "seed.txt").is_file()


def test_apply_refuses_a_dirty_nested_repository(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trash = tmp_path / "trash"
    nested = repo / "spikes/carabiner-link/upstream"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q", "-b", "main")
    _git(nested, "config", "user.email", "cleanup@example.test")
    _git(nested, "config", "user.name", "Cleanup Test")
    authored = nested / "authored.txt"
    authored.write_text("keep me\n")

    result = _run(repo, trash, "--apply")

    assert result.returncode == 3
    assert "uncommitted files" in result.stderr
    assert authored.read_text() == "keep me\n"
    assert not trash.exists()


def test_apply_allows_a_clean_standalone_downloaded_clone(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    trash = tmp_path / "trash"
    nested = repo / "spikes/carabiner-link/upstream"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q", "-b", "main")
    _git(nested, "config", "user.email", "cleanup@example.test")
    _git(nested, "config", "user.name", "Cleanup Test")
    downloaded = nested / "downloaded.txt"
    downloaded.write_text("regenerable\n")
    _git(nested, "add", "downloaded.txt")
    _git(nested, "commit", "-qm", "downloaded")

    result = _run(repo, trash, "--apply")

    assert result.returncode == 0, result.stderr
    assert not nested.exists()
    sessions = [path for path in trash.iterdir() if path.is_dir()]
    assert len(sessions) == 1
    assert (
        sessions[0] / "spikes/carabiner-link/upstream/downloaded.txt"
    ).read_text() == "regenerable\n"
