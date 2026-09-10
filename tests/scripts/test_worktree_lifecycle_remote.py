"""Characterize `_push_remote`: rsync when the host answers, keep local otherwise.

OPS-21 AC1's off-machine clause is the only production path the sibling suite
does not cover: every reap case there passes `--no-remote`. These cases call
`back_up(..., remote="nucbox-wsl")` against a throwaway worktree with real git,
real tar and real sha256. `subprocess.run` is stubbed so ssh/rsync never leave
the box. A restore that passed because nothing was there to lose is not a test;
`_make_dirty` plants modified, untracked and gitignored work plus a `.venv` and
`node_modules` the archive must not carry.
"""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts import worktree_census as census
from scripts import worktree_lifecycle as mod

pytestmark = pytest.mark.requirement("OPS-21")

BINARY_BLOB: bytes = bytes(range(256)) * 8


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return done.stdout


@pytest.fixture
def primary(tmp_path: Path) -> Path:
    """A committed repo standing in for the primary checkout."""
    repo = tmp_path / "primary"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("committed\n")
    (repo / ".gitignore").write_text(
        ".venv/\n.env\napps/desktop/src-tauri/gen/\napps/desktop/src-tauri/payload/\n"
    )
    _git(repo, "add", "README.md", ".gitignore")
    _git(repo, "commit", "-qm", "seed")
    return repo


def _add_worktree(primary: Path, path: Path, branch: str) -> None:
    _git(primary, "worktree", "add", "-q", str(path), "-b", branch)


def _make_dirty(worktree: Path) -> dict[str, bytes]:
    """Leave modified, untracked and non-regenerable-ignored files behind.

    Also plants a `.venv` and a `node_modules`: those are the files the archive
    must NOT carry, and a test that only planted work could not tell a correct
    exclusion from a broken walk.
    """
    contents = {
        "README.md": b"committed\nlocal edit nobody pushed\n",
        "notes/scratch.md": BINARY_BLOB,
        ".env": b"MUSIC_DJ_BACKEND_PORT=9999\n",
    }
    for rel, blob in contents.items():
        target = worktree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    (worktree / ".venv" / "lib").mkdir(parents=True)
    (worktree / ".venv" / "lib" / "big.bin").write_bytes(b"x" * 4096)
    (worktree / "node_modules" / "pkg").mkdir(parents=True)
    (worktree / "node_modules" / "pkg" / "index.js").write_text("regenerable\n")
    return contents


@pytest.fixture
def idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """No process holds any of these throwaway trees."""
    monkeypatch.setattr(census, "_liveness", lambda path, marker: ("idle", ""))


def _record(primary: Path, worktree: Path) -> census.Worktree:
    found = [
        w for w in mod.collect(primary, use_github=False, with_size=False)
        if w.path == worktree
    ]
    assert found, f"{worktree} not in the worktree list"
    return found[0]


def _prepare(
    primary: Path, tmp_path: Path, slug: str
) -> tuple[census.Worktree, Path, dict[str, bytes]]:
    worktree = tmp_path / f"wt-{slug}"
    _add_worktree(primary, worktree, f"af--{slug}")
    originals = _make_dirty(worktree)
    return _record(primary, worktree), tmp_path / f"backups-{slug}", originals


def _install_remote(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ssh_code: int = 0,
    ssh_out: str = "ok\n",
    ssh_err: str = "",
    rsync_code: int | None = 0,
) -> list[list[str]]:
    """Stub ssh/rsync. `rsync_code=None` means rsync must not be invoked."""
    calls: list[list[str]] = []

    def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = list(cmd)
        calls.append(argv)
        if argv[0] == "ssh":
            return subprocess.CompletedProcess(argv, ssh_code, ssh_out, ssh_err)
        if argv[0] == "rsync":
            if rsync_code is None:
                raise AssertionError(f"rsync must not run; got {argv}")
            err = "" if rsync_code == 0 else "rsync: connection unexpectedly closed\n"
            return subprocess.CompletedProcess(argv, rsync_code, "", err)
        raise AssertionError(f"unexpected subprocess {argv}")

    monkeypatch.setattr(mod.subprocess, "run", _run)
    return calls


def _packed(archive: Path) -> set[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return {member.name for member in tar.getmembers()}


def test_backup_rsyncs_when_the_remote_host_answers(
    primary: Path, tmp_path: Path, idle: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] nucbox-wsl answers [then] rsync lands the archive and the manifest, [else stop]."""
    record, backups, originals = _prepare(primary, tmp_path, "answers")
    calls = _install_remote(monkeypatch)

    manifest = mod.back_up(record, backups, remote="nucbox-wsl")

    archive = backups / manifest["archive"]
    stem = archive.name[: -len(mod.ARCHIVE_SUFFIX)]
    manifest_path = backups / f"{stem}.manifest.json"
    rsync_cmd = next(cmd for cmd in calls if cmd[0] == "rsync")
    assert rsync_cmd == [
        "rsync", "-a", str(archive), str(manifest_path), "nucbox-wsl:wt-backups/",
    ]
    assert manifest["remote"] == f"nucbox-wsl:wt-backups/{archive.name}"
    packed = _packed(archive)
    assert packed == set(originals), packed
    assert not [p for p in packed if p.startswith((".venv", "node_modules"))]


def test_backup_keeps_local_copy_when_the_remote_host_is_silent(
    primary: Path, tmp_path: Path, idle: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the remote does not answer [then] rsync never runs and local stays, [else stop]."""
    record, backups, originals = _prepare(primary, tmp_path, "silent")
    calls = _install_remote(
        monkeypatch, ssh_code=1, ssh_out="", ssh_err="ssh: Could not resolve hostname\n",
        rsync_code=None,
    )

    manifest = mod.back_up(record, backups, remote="nucbox-wsl")

    assert not any(cmd[0] == "rsync" for cmd in calls)
    assert "unreachable" in manifest["remote"]
    assert "local copy only" in manifest["remote"]
    archive = backups / manifest["archive"]
    stem = archive.name[: -len(mod.ARCHIVE_SUFFIX)]
    assert archive.is_file()
    assert (backups / f"{stem}.manifest.json").is_file()
    assert _packed(archive) == set(originals)


def test_backup_keeps_local_copy_when_rsync_fails(
    primary: Path, tmp_path: Path, idle: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] ssh works but rsync fails [then] the local archive is still kept, [else stop]."""
    record, backups, originals = _prepare(primary, tmp_path, "rsync-fail")
    _install_remote(monkeypatch, rsync_code=1)

    manifest = mod.back_up(record, backups, remote="nucbox-wsl")

    assert "rsync failed" in manifest["remote"]
    assert "local copy only" in manifest["remote"]
    archive = backups / manifest["archive"]
    stem = archive.name[: -len(mod.ARCHIVE_SUFFIX)]
    assert archive.is_file()
    assert (backups / f"{stem}.manifest.json").is_file()
    assert _packed(archive) == set(originals)
