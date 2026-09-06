"""Worktree reaper: prove the work SURVIVES, not merely that a reap ran.

Every case here is built on throwaway git repositories with real linked
worktrees, because the thing under test is a property of a worktree set and
cannot be staged inside the checkout under test.

The load-bearing case is `test_dirty_stale_worktree_is_archived_then_restored`.
A reaper that deletes is easy; a reaper that deletes only what it has already
proved it can hand back is the whole requirement, so that test compares restored
bytes to the originals rather than asserting a backup step was reached. Deleting
`back_up`'s verification turns it red -- that mutation is recorded in the PR.

  - [if] a stale worktree is reaped [then] its work comes back byte-for-byte, [else stop].
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts import worktree_census as census
from scripts import worktree_lifecycle as mod

pytestmark = pytest.mark.requirement("OPS-21")

HOUR: int = 3600
STALE_AGE_H: int = 100          # comfortably past the 72h window
ACTIVE_AGE_H: int = 2
BINARY_BLOB: bytes = bytes(range(256)) * 8   # catches a text-mode restore


# -----------------------------------------------------------------------------
# _helpers
# -----------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return done.stdout


def _age(root: Path, hours: float) -> None:
    """Backdate every file the last-touch scan can see under `root`."""
    when = mod.dt.datetime.now(mod.dt.timezone.utc).timestamp() - hours * HOUR
    for entry in census._walk_paths(root):
        os.utime(entry.path, (when, when))


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


def _reap(primary: Path, backup_root: Path, *extra: str) -> int:
    return mod.main([
        "reap", "--repo", str(primary), "--backup-root", str(backup_root),
        "--no-github", "--no-remote", *extra,
    ])


@pytest.fixture
def idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """No process holds any of these throwaway trees.

    Stubbed rather than measured: a real `lsof +D` over tmp_path is slow and its
    answer is environmental, so leaving it live would make every case here a
    test of the sandbox. `_liveness` gets its own cases below.
    """
    monkeypatch.setattr(census, "_liveness", lambda path, marker: ("idle", ""))


# -----------------------------------------------------------------------------
# _reap: the work survives
# -----------------------------------------------------------------------------


def test_dirty_stale_worktree_is_archived_then_restored(
    primary: Path, tmp_path: Path, idle: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a stale dirty worktree is reaped [then] a restore returns every byte, [else stop]."""
    worktree = tmp_path / "wt-dirty"
    _add_worktree(primary, worktree, "af--dirty")
    originals = _make_dirty(worktree)
    _age(worktree, STALE_AGE_H)
    backups = tmp_path / "backups"

    assert _reap(primary, backups, "--apply") == 0
    assert not worktree.exists(), "the stale worktree should be gone"

    manifests = list(backups.glob("af--dirty-*.manifest.json"))
    assert len(manifests) == 1, f"expected one manifest, found {manifests}"
    manifest = mod.json.loads(manifests[0].read_text())
    assert set(manifest["paths"]) == set(originals), (
        f"archive paths {sorted(manifest['paths'])} do not match the "
        f"uncommitted set {sorted(originals)}"
    )
    assert not [p for p in manifest["paths"] if p.startswith((".venv", "node_modules"))]

    restored = tmp_path / "wt-restored"
    assert mod.main([
        "restore", "af--dirty", "--repo", str(primary),
        "--backup-root", str(backups), "--into", str(restored),
    ]) == 0
    for rel, blob in originals.items():
        assert (restored / rel).read_bytes() == blob, f"{rel} did not survive the round trip"
    assert "digests match" in capsys.readouterr().out


@pytest.mark.parametrize(("path", "regenerable"), [
    ("apps/desktop/src-tauri/gen", True),            # the COLLAPSED directory form
    ("apps/desktop/src-tauri/gen/", True),
    ("apps/desktop/src-tauri/gen/schemas/x.json", True),
    ("apps/desktop/src-tauri/payload", True),
    ("apps/desktop/src-tauri/generated-notes.md", False),   # merely starts the same
    ("apps/desktop/src-tauri/notes.md", False),
    (".grimp_cache/abc.json", True),
    ("music_dj_tools.egg-info/PKG-INFO", True),
    ("apps/webui/gen.py", False),
])
def test_regenerable_matches_the_directory_itself_not_just_paths_under_it(
    path: str, regenerable: bool
) -> None:
    """[if] an ignored tree collapses to its directory name [then] it stays excluded, [else stop].

    The first real run missed exactly this: `ls-files --others --ignored
    --directory` emits `apps/desktop/src-tauri/gen`, with no trailing slash, and
    a prefix test written as `startswith("apps/desktop/src-tauri/gen/")` is False
    for it. 10,436 paths were archived that should not have been, while a test
    that only used the file form passed.
    """
    assert census._is_regenerable(path) is regenerable


def test_generated_build_output_is_not_mistaken_for_work(
    primary: Path, tmp_path: Path, idle: None
) -> None:
    """[if] a worktree holds generated output [then] only real work is archived, [else stop]."""
    worktree = tmp_path / "wt-generated"
    _add_worktree(primary, worktree, "af--generated")
    originals = _make_dirty(worktree)
    generated = [
        "apps/desktop/src-tauri/gen/schemas/capabilities.json",   # path-prefix rule
        "apps/desktop/src-tauri/payload/Open DJ.app/Info.plist",
        ".grimp_cache/abc.data.json",                             # name rule
        "music_dj_tools.egg-info/PKG-INFO",                       # suffix rule
    ]
    kept_work = "apps/desktop/src-tauri/notes.md"                 # SAME apps/ subtree
    for rel in [*generated, kept_work]:
        target = worktree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("generated\n")
    _age(worktree, STALE_AGE_H)
    backups = tmp_path / "backups"

    assert _reap(primary, backups, "--apply") == 0
    manifest = mod.json.loads(next(backups.glob("*.manifest.json")).read_text())
    packed = set(manifest["paths"])
    assert not packed & set(generated), f"generated output was archived: {packed & set(generated)}"
    # The positive control: an exclusion that swallowed the whole apps/ subtree
    # would pass the assertion above for the wrong reason.
    assert kept_work in packed, "real untracked work under apps/ must still be saved"
    assert set(originals) <= packed


def test_reap_refuses_when_the_archive_is_incomplete(
    primary: Path, tmp_path: Path, idle: None
) -> None:
    """[if] a path is missing from the archive [then] the worktree is left standing, [else stop]."""
    worktree = tmp_path / "wt-dirty"
    _add_worktree(primary, worktree, "af--dirty")
    _make_dirty(worktree)
    _age(worktree, STALE_AGE_H)
    real_add = tarfile.TarFile.add

    def _lossy_add(self, name, arcname=None, **kwargs):
        if arcname == "notes/scratch.md":
            return None
        return real_add(self, name, arcname=arcname, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(tarfile.TarFile, "add", _lossy_add)
        with pytest.raises(RuntimeError, match="missing 1 path"):
            _reap(primary, tmp_path / "backups", "--apply")
    assert worktree.exists(), "a failed backup must never be followed by a removal"
    assert (worktree / "notes" / "scratch.md").exists()


def test_dry_run_changes_nothing(primary: Path, tmp_path: Path, idle: None,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    """[if] reap runs with --dry-run [then] the worktree and disk are untouched, [else stop]."""
    worktree = tmp_path / "wt-dirty"
    _add_worktree(primary, worktree, "af--dirty")
    _make_dirty(worktree)
    _age(worktree, STALE_AGE_H)
    backups = tmp_path / "backups"

    assert _reap(primary, backups, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "[reap DRY-RUN] 1 of" in out
    assert worktree.exists()
    assert not backups.exists(), "a dry run must not create the backup store"


# -----------------------------------------------------------------------------
# _classify: what is kept, and why
# -----------------------------------------------------------------------------


def _verdict(primary: Path, worktree: Path, **kwargs) -> mod.Verdict:
    found = [w for w in mod.collect(primary, use_github=kwargs.pop("use_github", False),
                                    with_size=False)
             if w.path == worktree]
    assert found, f"{worktree} not in the worktree list"
    return mod.classify(found[0], mod.STALE_HOURS)


def test_active_worktree_is_never_reaped(primary: Path, tmp_path: Path, idle: None,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    """[if] a worktree was touched inside the window [then] it is KEEP active, [else stop]."""
    worktree = tmp_path / "wt-active"
    _add_worktree(primary, worktree, "af--active")
    _make_dirty(worktree)
    _age(worktree, ACTIVE_AGE_H)

    verdict = _verdict(primary, worktree)
    assert verdict.action == "KEEP" and "active" in verdict.reason, verdict
    assert _reap(primary, tmp_path / "backups", "--apply") == 0
    assert worktree.exists()
    assert "0 of" in capsys.readouterr().out


def test_stale_worktree_with_an_open_pr_is_kept(primary: Path, tmp_path: Path,
                                                idle: None,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a stale branch still has an open PR [then] the verdict is KEEP pr-open, [else stop]."""
    worktree = tmp_path / "wt-pr"
    _add_worktree(primary, worktree, "af--open-pr")
    _age(worktree, STALE_AGE_H)
    monkeypatch.setattr(census, "_pr_index",
                        lambda use_github: {"af--open-pr": census.PullRequest(7, "OPEN")})

    verdict = _verdict(primary, worktree, use_github=True)
    assert verdict.action == "KEEP" and "pr-open (#7)" in verdict.reason, verdict


def test_stale_worktree_with_a_merged_pr_is_reaped(primary: Path, tmp_path: Path,
                                                   idle: None,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a stale branch's PR is merged [then] the verdict is REAP, [else stop]."""
    worktree = tmp_path / "wt-merged"
    _add_worktree(primary, worktree, "af--merged-pr")
    _age(worktree, STALE_AGE_H)
    monkeypatch.setattr(census, "_pr_index",
                        lambda use_github: {"af--merged-pr": census.PullRequest(9, "MERGED")})

    verdict = _verdict(primary, worktree, use_github=True)
    assert verdict.action == "REAP" and "#9 merged" in verdict.reason, verdict


def test_live_worktree_is_kept_whatever_its_age(primary: Path, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a process holds a stale worktree [then] the verdict is KEEP live, [else stop]."""
    worktree = tmp_path / "wt-live"
    _add_worktree(primary, worktree, "af--live")
    _age(worktree, STALE_AGE_H)
    monkeypatch.setattr(census, "_liveness", lambda path, marker: ("live", "pgrep pids 4242"))

    verdict = _verdict(primary, worktree)
    assert verdict.action == "KEEP" and "live" in verdict.reason, verdict


def test_unmeasurable_liveness_keeps_the_worktree(primary: Path, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] lsof cannot answer [then] the verdict is KEEP, never REAP, [else stop]."""
    worktree = tmp_path / "wt-unknown"
    _add_worktree(primary, worktree, "af--unknown")
    _age(worktree, STALE_AGE_H)

    def _timeout(cmd, **kwargs):
        if cmd[0] == "lsof":
            raise subprocess.TimeoutExpired(cmd, census.LSOF_TIMEOUT_S)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(census.subprocess, "run", _timeout)
    state, detail = census._liveness(worktree, "worktree_lifecycle.py")
    assert state == "unknown" and "timed out" in detail
    assert mod.classify(
        dataclasses.replace(
            census.Worktree(path=worktree, branch="af--unknown", head="deadbeef",
                         is_primary=False, exists=True, last_touch=0.0, dirty_paths=[],
                         kept_ignored=[], size_kb=0, pr=None, liveness=state,
                         live_detail=detail)),
        mod.STALE_HOURS,
    ).action == "KEEP"


def test_git_own_fsmonitor_daemon_does_not_read_as_a_user(
    primary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] only git's fsmonitor daemon holds a worktree [then] it is not live, [else stop]."""
    worktree = tmp_path / "wt-fsmon"
    _add_worktree(primary, worktree, "af--fsmon")
    _age(worktree, STALE_AGE_H)
    fsmon, real = "4242", "4243"
    commands = {
        fsmon: "/opt/homebrew/opt/git/libexec/git-core/git fsmonitor--daemon run --detach",
        real: "node /some/agent/server.js",
        "4244": "",                      # exited between the probe and the ps
    }

    def _fake(cmd, **kwargs):
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(cmd, 1, "", "")
        if cmd[0] == "lsof":
            return subprocess.CompletedProcess(cmd, 0, "\n".join(pids_seen) + "\n", "")
        if cmd[0] == "ps":
            return subprocess.CompletedProcess(cmd, 0, commands[cmd[-1]], "")
        raise AssertionError(f"unexpected probe {cmd}")

    monkeypatch.setattr(census.subprocess, "run", _fake)
    monkeypatch.setattr(census.shutil, "which", lambda name: "/usr/bin/lsof")

    pids_seen = [fsmon, "4244"]
    assert census._liveness(worktree, "worktree_lifecycle.py") == ("idle", ""), (
        "git's own watcher and a dead pid must not keep a finished worktree alive"
    )
    # The same probe MUST still fire for a real one, or the exclusion is just a
    # blindfold: an lsof that ignores everything and one that found nothing are
    # the same empty answer.
    pids_seen = [fsmon, real]
    state, detail = census._liveness(worktree, "worktree_lifecycle.py")
    assert state == "live" and detail == f"lsof pids {real}", (state, detail)


def test_primary_checkout_is_never_reaped(primary: Path, tmp_path: Path, idle: None) -> None:
    """[if] the primary checkout is stale and dirty [then] it is still KEEP, [else stop]."""
    (primary / "uncommitted.txt").write_text("work in the shared tree\n")
    _age(primary, STALE_AGE_H)

    verdict = _verdict(primary, primary)
    assert verdict.action == "KEEP" and "primary" in verdict.reason, verdict
    assert _reap(primary, tmp_path / "backups", "--apply") == 0
    assert (primary / "uncommitted.txt").exists()


def test_regenerable_output_does_not_look_like_a_human_touch(
    primary: Path, tmp_path: Path, idle: None
) -> None:
    """[if] only .venv was written recently [then] the worktree is still stale, [else stop]."""
    worktree = tmp_path / "wt-synced"
    _add_worktree(primary, worktree, "af--synced")
    _make_dirty(worktree)
    _age(worktree, STALE_AGE_H)
    os.utime(worktree / ".venv" / "lib" / "big.bin", None)   # a fresh `uv sync`

    assert _verdict(primary, worktree).action == "REAP"


# -----------------------------------------------------------------------------
# _guard: the cap and the floor
# -----------------------------------------------------------------------------


def _guard(primary: Path, **kwargs: object) -> int:
    argv = ["guard", "--repo", str(primary)]
    for key, value in kwargs.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return mod.main(argv)


def test_guard_refuses_loudly_at_the_cap(primary: Path, tmp_path: Path,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    """[if] live worktrees reach the cap [then] guard exits nonzero naming reap, [else stop]."""
    for index in range(2):
        _add_worktree(primary, tmp_path / f"wt-{index}", f"af--{index}")

    assert _guard(primary, cap=2, floor_gb=0) == 1
    err = capsys.readouterr().err
    assert "worktree cap reached: 2 live, cap 2" in err
    assert mod.REAP_COMMAND in err


def test_guard_refuses_loudly_below_the_disk_floor(primary: Path, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    """[if] free disk is under the floor [then] guard exits nonzero naming it, [else stop]."""
    monkeypatch.setattr(mod, "free_gb", lambda path: 4.6)

    assert _guard(primary, cap=99, floor_gb=15) == 1
    err = capsys.readouterr().err
    assert "disk floor breached: 4.6G free, floor 15G" in err
    assert "Reap until 25G is free" in err
    assert mod.REAP_COMMAND in err


def test_guard_passes_under_the_cap_and_over_the_floor(
    primary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] the cap and the floor are both satisfied [then] guard exits 0, [else stop]."""
    _add_worktree(primary, tmp_path / "wt-0", "af--0")
    monkeypatch.setattr(mod, "free_gb", lambda path: 120.0)

    assert _guard(primary, cap=10, floor_gb=15) == 0
    assert "[wt-guard] OK: 1 live worktree(s) of 10" in capsys.readouterr().out


# -----------------------------------------------------------------------------
# _registry and _status
# -----------------------------------------------------------------------------


def test_registry_records_owner_and_keeps_first_seen(primary: Path, tmp_path: Path,
                                                     idle: None) -> None:
    """[if] register runs twice [then] first_seen is preserved and owner inferred, [else stop]."""
    _add_worktree(primary, tmp_path / "wt-codex", "codex/some-lane")
    argv = ["register", "--repo", str(primary), "--no-github", "--no-size"]

    assert mod.main(argv) == 0
    registry = mod.registry_path(primary)
    first = {row["path"]: row for row in mod.json.loads(registry.read_text())["worktrees"]}
    assert first[str(tmp_path / "wt-codex")]["owner"] == "codex"

    assert mod.main(argv) == 0
    second = {row["path"]: row for row in mod.json.loads(registry.read_text())["worktrees"]}
    assert second[str(tmp_path / "wt-codex")]["first_seen"] == \
        first[str(tmp_path / "wt-codex")]["first_seen"]
    assert second[str(tmp_path / "wt-codex")]["last_scan"] != ""


def test_status_table_names_every_worktree_and_its_verdict(
    primary: Path, tmp_path: Path, idle: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] status runs [then] each worktree gets a row with a verdict, [else stop]."""
    _add_worktree(primary, tmp_path / "wt-stale", "af--stale")
    _age(tmp_path / "wt-stale", STALE_AGE_H)

    assert mod.main(["status", "--repo", str(primary), "--no-github", "--no-size"]) == 0
    out = capsys.readouterr().out
    assert "af--stale" in out and "REAP:" in out
    assert "KEEP: primary checkout" in out


def test_restore_without_a_backup_fails_loudly(primary: Path, tmp_path: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    """[if] no archive exists for a branch [then] restore exits 1 saying so, [else stop]."""
    assert mod.main(["restore", "af--never-saved", "--repo", str(primary),
                     "--backup-root", str(tmp_path / "backups")]) == 1
    assert "no backup for branch" in capsys.readouterr().err


def test_backup_and_restore_need_no_host_binary(
    primary: Path, tmp_path: Path, idle: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] no compression binary is on PATH [then] the round trip still works, [else stop].

    The regression this pins: back_up shelled out to `zstd`, the nucbox pytest
    runners have no zstd, and three cases died on FileNotFoundError with trunk
    red behind them. PATH is REPLACED with a directory holding git and nothing
    else, rather than trusted to be bare: the developer machine writing this
    test almost certainly HAS zstd, so a case that merely ran green locally
    would prove nothing about a runner that does not. git stays because this
    module is a git tool and cannot pretend otherwise; every other host binary
    is gone, so re-introducing any of them turns this red. `--no-size` is passed
    for the same reason: the size column shells out to `du`, which is coreutils
    and present on every runner, so leaving it in would make this case fail for
    a reason it is not about.
    """
    only_git = tmp_path / "path-with-only-git"
    only_git.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None, "no git on PATH; this case cannot be set up"
    (only_git / "git").symlink_to(real_git)
    monkeypatch.setenv("PATH", str(only_git))
    assert shutil.which("zstd") is None, "PATH masking failed; this case would prove nothing"
    worktree = tmp_path / "wt-no-binaries"
    _add_worktree(primary, worktree, "af--no-binaries")
    originals = _make_dirty(worktree)
    _age(worktree, STALE_AGE_H)
    backups = tmp_path / "backups"

    assert _reap(primary, backups, "--apply", "--no-size") == 0
    archive = next(backups.glob("af--no-binaries-*.tar.gz"))
    assert tarfile.is_tarfile(archive), f"{archive.name} is not a readable tar"

    restored = tmp_path / "wt-no-binaries-back"
    assert mod.main([
        "restore", "af--no-binaries", "--repo", str(primary),
        "--backup-root", str(backups), "--into", str(restored),
    ]) == 0
    for rel, blob in originals.items():
        assert (restored / rel).read_bytes() == blob, f"{rel} did not survive the round trip"


def _write_legacy_zstd_backup(backups: Path, worktree: Path, label: str) -> dict[str, bytes]:
    """Hand-build a pre-#1387-format `.tar.zst` backup plus its manifest."""
    backups.mkdir(parents=True, exist_ok=True)
    originals = {"legacy.txt": b"legacy work\n", "legacy.bin": BINARY_BLOB}
    for rel, blob in originals.items():
        (worktree / rel).write_bytes(blob)
    plain = backups / f"{label}-20260101T000000Z.tar"
    with tarfile.open(plain, "w") as tar:
        for rel in originals:
            tar.add(worktree / rel, arcname=rel, recursive=False)
    archive = plain.with_suffix(".tar.zst")
    subprocess.run(["zstd", "-q", "-19", "-f", str(plain), "-o", str(archive)], check=True)
    plain.unlink()
    (backups / f"{label}-20260101T000000Z.manifest.json").write_text(mod.json.dumps({
        "branch": label,
        "head": "0" * 40,
        "worktree": str(worktree),
        "archive": archive.name,
        "created_utc": "2026-01-01T00:00:00+00:00",
        "paths": sorted(originals),
        "sha256": {rel: hashlib.sha256(blob).hexdigest() for rel, blob in originals.items()},
        "archive_bytes": archive.stat().st_size,
    }))
    return originals


@pytest.mark.skipif(shutil.which("zstd") is None, reason="legacy reader needs the zstd binary")
def test_legacy_zstd_archive_still_restores(primary: Path, tmp_path: Path) -> None:
    """[if] a backup predates the gzip switch [then] restore still returns it, [else stop].

    ~130 `.tar.zst` archives already exist under ~/.cache/mdt-worktree-backups
    and on nucbox-wsl:~/wt-backups. Dropping the reader would strand real
    uncommitted work, so the format switch is write-only.
    """
    staging = tmp_path / "staging"
    staging.mkdir()
    _git(primary, "branch", "af--legacy")
    backups = tmp_path / "backups"
    originals = _write_legacy_zstd_backup(backups, staging, "af--legacy")

    restored = tmp_path / "wt-legacy"
    assert mod.main(["restore", "af--legacy", "--repo", str(primary),
                     "--backup-root", str(backups), "--into", str(restored)]) == 0
    for rel, blob in originals.items():
        assert (restored / rel).read_bytes() == blob, f"legacy {rel} did not survive"


def test_legacy_zstd_archive_without_the_binary_names_it(
    primary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] a .tar.zst is restored with no zstd on PATH [then] it fails naming it, [else stop].

    The overshoot this guards against is a silent fallback: an empty or partial
    restore that exits 0 is worse than a refusal, because the worktree is
    already gone by then.

    Deliberately NOT skipped when zstd is absent, and the archive is a STUB of
    arbitrary bytes rather than a real zstd stream. The guard fires on
    `which("zstd")` before anything is decompressed, so the contents are
    irrelevant -- and a skipif here would skip this case on precisely the hosts
    whose missing zstd it exists to describe.
    """
    _git(primary, "branch", "af--legacy-nozstd")
    backups = tmp_path / "backups"
    backups.mkdir()
    stub = backups / "af--legacy-nozstd-20260101T000000Z.tar.zst"
    stub.write_bytes(b"not a real zstd frame; never reached")
    (backups / "af--legacy-nozstd-20260101T000000Z.manifest.json").write_text(mod.json.dumps({
        "branch": "af--legacy-nozstd", "head": "0" * 40,
        "worktree": str(tmp_path / "gone"), "archive": stub.name,
        "created_utc": "2026-01-01T00:00:00+00:00", "paths": ["legacy.txt"],
        "sha256": {"legacy.txt": "0" * 64}, "archive_bytes": stub.stat().st_size,
    }))

    monkeypatch.setattr(mod.shutil, "which", lambda name: None if name == "zstd" else "/bin/true")
    restored = tmp_path / "wt-legacy-nozstd"
    assert mod.main(["restore", "af--legacy-nozstd", "--repo", str(primary),
                     "--backup-root", str(backups), "--into", str(restored)]) == 1
    err = capsys.readouterr().err
    # Assert the GUARD's own wording, not merely the substring "zstd". Both this
    # message and the generic "zstd -d failed for <archive>" contain "zstd", and
    # the archive name here contains "legacy", so the obvious pair of assertions
    # passes for either path -- measured, not assumed: with the `which` guard
    # stubbed out so the subprocess runs and fails on the stub bytes, the weaker
    # assertions stayed green. That is a test that cannot fail for the reason it
    # exists. These two discriminate: the first is unique to the guard, and the
    # second refuses the fall-through message outright.
    assert "not on PATH" in err, f"the error must name the missing binary; got: {err}"
    assert "zstd -d failed" not in err, (
        f"the guard must refuse BEFORE shelling out, not report a subprocess failure; got: {err}"
    )


def test_manifest_digests_are_of_the_real_bytes(primary: Path, tmp_path: Path,
                                                idle: None) -> None:
    """[if] a manifest records a sha256 [then] it is the file's own digest, [else stop]."""
    worktree = tmp_path / "wt-digest"
    _add_worktree(primary, worktree, "af--digest")
    originals = _make_dirty(worktree)
    _age(worktree, STALE_AGE_H)
    backups = tmp_path / "backups"

    assert _reap(primary, backups, "--apply") == 0
    manifest = mod.json.loads(next(backups.glob("*.manifest.json")).read_text())
    for rel, blob in originals.items():
        assert manifest["sha256"][rel] == hashlib.sha256(blob).hexdigest()
