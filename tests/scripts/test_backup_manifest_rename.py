"""The music-backup manifest has one role-named path; a legacy file is linked once (issue #5639).

The local manifest used to be named for the house store alias. Since the store
host is config, it is ``(private path)`` for the
writer and the reader alike, and a checkout holding only the legacy file has it
linked to it on first run ((private path)).

Regression one-liners:
  - if the writer and the reader disagree on the manifest path then broken
  - if a legacy-only manifest is not linked with its bytes intact then broken
  - if both names present is not refused naming both then broken
  - if a symlink at either manifest name is accepted as a finished hardlink then broken
  - if a name swapped for a FIFO or a symlink after the entry check is adopted then broken
  - if a current manifest created mid-migration is replaced then broken
  - if success is reported while the two names are different files then broken
  - if the success line is printed by a run that refuses then broken
  - if a legacy name rebound before the link is adopted as migrated then broken
  - if a legacy name removed before the link is not the documented refusal then broken
  - if a current name that is gone is reported as migrated then broken
  - if the pre-link handle is closed before verification then broken
  - if the migration deletes any directory entry then broken
  - if the legacy name vanishes mid-migration and its bytes are lost then broken
  - if a filesystem without hardlinks is renamed over instead then broken
  - if a checkout with no legacy file is touched then broken
  - if the store-listing error label does not follow MDT_B2STORE_SSH_HOST then broken
"""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts import backup_manifest
from scripts.b2store import B2STORE_SSH_HOST_ENV
from scripts.backup_manifest import LEGACY_MUSIC_MANIFEST_REL, MUSIC_MANIFEST_REL, migrate_music_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = '{"tracks": {}, "skipped_evicted": ["/Users/user/Music/a.mp3"]}'


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _symlink_or_skip(target: str, link: Path) -> None:
    """Create ``link`` as a relative symlink, skipping where the platform forbids one."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this platform cannot create a symlink ({exc})")


def _migrate_with_a_deadline(root: Path, seconds: float = 5.0) -> BaseException | None:
    """Run the migration on a daemon thread; fail rather than hang if it blocks.

    A non-regular entry under the old module made ``os.open`` wait forever, so a
    test that simply called the migration would hang the suite instead of
    reporting a regression. Returns whatever the call raised.
    """
    raised: list[BaseException | None] = [None]

    def run() -> None:
        try:
            migrate_music_manifest(root)
        except BaseException as exc:  # noqa: BLE001 - the exception itself is the measurement
            raised[0] = exc

    thread = threading.Thread(target=run, name="migrate-entry-probe", daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), f"the migration was still blocked after {seconds}s"
    return raised[0]


def test_manifest_is_named_for_its_role() -> None:
    """[if] the manifest path still names a host [then] fail, [else stop]."""
    assert Path("(private path)") == MUSIC_MANIFEST_REL
    assert Path("(private path)") == LEGACY_MUSIC_MANIFEST_REL


def test_legacy_only_manifest_gains_the_current_name_with_its_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a legacy-only manifest is not given the current name intact [then] fail, [else stop]."""
    _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)
    legacy = tmp_path / LEGACY_MUSIC_MANIFEST_REL
    current = tmp_path / MUSIC_MANIFEST_REL
    assert migrate_music_manifest(tmp_path) == current
    assert current.read_text(encoding="utf-8") == PAYLOAD
    # One file under two names: nothing was deleted, so nothing could be lost.
    assert legacy.exists()
    assert os.stat(legacy).st_ino == os.stat(current).st_ino
    assert os.stat(legacy).st_dev == os.stat(current).st_dev
    # Second run: already migrated, not an error, nothing changes.
    assert migrate_music_manifest(tmp_path) == current
    assert current.read_text(encoding="utf-8") == PAYLOAD
    # The documented outcome is printed exactly once, by the run that linked
    # the file; the already-migrated rerun must not repeat it.
    assert capsys.readouterr().out == f"linked {legacy} -> {current} (#5639)\n"


def test_both_names_present_is_refused_naming_both(tmp_path: Path) -> None:
    """[if] both manifests exist and one is silently picked [then] fail, [else stop]."""
    _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)
    _write(tmp_path / MUSIC_MANIFEST_REL, "{}")
    with pytest.raises(FileExistsError) as excinfo:
        migrate_music_manifest(tmp_path)
    message = str(excinfo.value)
    assert str(tmp_path / LEGACY_MUSIC_MANIFEST_REL) in message
    assert str(tmp_path / MUSIC_MANIFEST_REL) in message
    assert (tmp_path / LEGACY_MUSIC_MANIFEST_REL).read_text(encoding="utf-8") == PAYLOAD
    assert (tmp_path / MUSIC_MANIFEST_REL).read_text(encoding="utf-8") == "{}"


def test_symlinked_current_is_not_accepted_as_already_migrated(tmp_path: Path) -> None:
    """[if] a symlinked current name is counted as a finished hardlink [then] fail, [else stop].

    The reviewer's P2 on PR #7443: a following ``os.open`` gave the symlink its
    target's identity, so the migration reported success, and the documented
    cleanup (removing the legacy name) then left the current name dangling.
    """
    _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)
    legacy = tmp_path / LEGACY_MUSIC_MANIFEST_REL
    current = tmp_path / MUSIC_MANIFEST_REL
    _symlink_or_skip(LEGACY_MUSIC_MANIFEST_REL.name, current)
    assert current.is_symlink()

    # No "already migrated" return: an alias is not a pair of names for one file.
    with pytest.raises(FileExistsError) as excinfo:
        migrate_music_manifest(tmp_path)
    message = str(excinfo.value)
    assert str(legacy) in message
    assert str(current) in message
    assert "symlink" in message
    # Nothing was written, so the entry the operator left there is still there,
    # and no hardlink exists between the two names.
    assert current.is_symlink()
    assert os.lstat(current).st_nlink == 1
    assert legacy.read_text(encoding="utf-8") == PAYLOAD
    assert current.read_text(encoding="utf-8") == PAYLOAD

    # The refusal is recoverable the way its message says: a real file under the
    # current name migrates normally.
    current.unlink()
    assert migrate_music_manifest(tmp_path) == current
    assert os.stat(legacy).st_ino == os.stat(current).st_ino
    assert not current.is_symlink()


def test_symlinked_legacy_name_is_refused_and_nothing_is_created(tmp_path: Path) -> None:
    """[if] a symlinked legacy name is followed and linked [then] fail, [else stop]."""
    real = tmp_path / "elsewhere" / "manifest.json"
    _write(real, PAYLOAD)
    legacy = tmp_path / LEGACY_MUSIC_MANIFEST_REL
    current = tmp_path / MUSIC_MANIFEST_REL
    # A relative link, exactly like the current-name case that dangles.
    _symlink_or_skip(os.path.join("..", "elsewhere", "manifest.json"), legacy)
    assert legacy.is_symlink()

    with pytest.raises(FileExistsError) as excinfo:
        migrate_music_manifest(tmp_path)
    message = str(excinfo.value)
    assert str(legacy) in message
    assert str(current) in message
    # The link syscall never ran, so nothing was created under the current name
    # (and the target underneath the link is untouched).
    assert not current.exists()
    assert legacy.is_symlink()
    assert real.read_text(encoding="utf-8") == PAYLOAD


@pytest.mark.parametrize("which", ["legacy", "current"], ids=["legacy", "current"])
def test_a_directory_at_either_name_is_a_non_regular_refusal(tmp_path: Path, which: str) -> None:
    """[if] a directory stands where a manifest name belongs and is opened [then] fail, [else stop]."""
    entry = tmp_path / (LEGACY_MUSIC_MANIFEST_REL if which == "legacy" else MUSIC_MANIFEST_REL)
    entry.mkdir(parents=True)
    if which == "current":
        _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)

    with pytest.raises(FileExistsError) as excinfo:
        migrate_music_manifest(tmp_path)
    message = str(excinfo.value)
    assert str(tmp_path / LEGACY_MUSIC_MANIFEST_REL) in message
    assert str(tmp_path / MUSIC_MANIFEST_REL) in message
    assert "non-regular" in message
    assert entry.is_dir()
    assert not (tmp_path / MUSIC_MANIFEST_REL).is_file()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFO to block a bare open")
@pytest.mark.parametrize("which", ["legacy", "current"], ids=["legacy", "current"])
def test_a_fifo_at_either_name_is_refused_without_blocking(tmp_path: Path, which: str) -> None:
    """[if] a FIFO at a manifest name blocks the migration's open [then] fail, [else stop].

    A bare ``os.open(O_RDONLY)`` on a FIFO with no writer waits forever, so the
    entry-type check has to run BEFORE the open. The deadline below is what makes
    a regression fail this test instead of hanging the suite.
    """
    entry = tmp_path / (LEGACY_MUSIC_MANIFEST_REL if which == "legacy" else MUSIC_MANIFEST_REL)
    entry.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(entry)
    if which == "current":
        _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)

    raised = _migrate_with_a_deadline(tmp_path)

    assert isinstance(raised, FileExistsError), f"expected the entry refusal, got {raised!r}"
    message = str(raised)
    assert str(tmp_path / LEGACY_MUSIC_MANIFEST_REL) in message
    assert str(tmp_path / MUSIC_MANIFEST_REL) in message
    assert "non-regular" in message
    assert stat.S_ISFIFO(os.lstat(entry).st_mode)
    assert not (tmp_path / MUSIC_MANIFEST_REL).is_file()


def _swap_at_open(
    monkeypatch: pytest.MonkeyPatch, target: Path, swap: Callable[[], None]
) -> dict[str, bool]:
    """Run ``swap`` the first time ``target`` is opened, inside the lstat->open window.

    The window is two syscalls wide, so this is the only way to reproduce it
    without a second process racing the test: the name is re-bound by another
    writer after the entry check and before the open that would inspect it.
    """
    real_open = os.open
    state = {"done": False}

    def open_after_the_swap(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == target and not state["done"]:
            state["done"] = True
            swap()
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(backup_manifest.os, "open", open_after_the_swap)
    return state


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFO to block a bare open")
@pytest.mark.parametrize("which", ["legacy", "current"], ids=["legacy", "current"])
def test_a_name_swapped_for_a_fifo_before_the_open_is_refused_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    """[if] a name becomes a FIFO between the lstat and the open [then] fail, [else stop].

    ``O_NOFOLLOW`` does not help here - a FIFO is not a link - and the entry
    check that refuses a FIFO cannot run once the open is waiting for a writer
    that never comes. ``O_NONBLOCK`` is what lets the open return so ``fstat``
    sees the entry type; the deadline is what makes a regression fail this test
    instead of hanging the suite.
    """
    _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)
    legacy = tmp_path / LEGACY_MUSIC_MANIFEST_REL
    current = tmp_path / MUSIC_MANIFEST_REL
    entry = legacy if which == "legacy" else current

    def swap_to_fifo() -> None:
        entry.unlink()
        os.mkfifo(entry)

    state = _swap_at_open(monkeypatch, entry, swap_to_fifo)
    raised = _migrate_with_a_deadline(tmp_path)

    assert state["done"]
    assert isinstance(raised, FileExistsError), f"expected the entry refusal, got {raised!r}"
    message = str(raised)
    assert str(legacy) in message
    assert str(current) in message
    assert "non-regular" in message
    assert stat.S_ISFIFO(os.lstat(entry).st_mode)


@pytest.mark.parametrize("which", ["legacy", "current"], ids=["legacy", "current"])
@pytest.mark.parametrize("no_follow", [True, False], ids=["o-nofollow", "without-o-nofollow"])
def test_a_name_swapped_for_a_symlink_before_the_open_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str, no_follow: bool
) -> None:
    """[if] a name becomes a symlink between the lstat and the open [then] fail, [else stop].

    The second parameter is the arm this checkout does not have: ``O_NOFOLLOW``
    is removed from the module's view to stand in for Windows, so the refusal is
    exercised on the path where the flag cannot supply it. That arm is the one
    that used to report success, because the swap happens after the entry check
    and, when the link points at the expected file, the handle and the inspected
    entry carry the SAME inode - only re-inspecting the name sees the link.
    """
    if not no_follow:
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)
    legacy = tmp_path / LEGACY_MUSIC_MANIFEST_REL
    current = tmp_path / MUSIC_MANIFEST_REL
    entry = legacy if which == "legacy" else current

    def swap_to_symlink() -> None:
        if entry.exists() or entry.is_symlink():
            entry.unlink()
        if which == "legacy":
            # Dangling, so an open without O_NOFOLLOW reports it exactly like a
            # name that was removed: the refusal has to re-inspect the name to
            # tell those apart.
            _symlink_or_skip("bifrost2-music-manifest-gone.json", entry)
        else:
            # The alias this migration refuses, pointing at the very file it
            # just linked: every identity comparison agrees with it.
            _symlink_or_skip(legacy.name, entry)

    state = _swap_at_open(monkeypatch, entry, swap_to_symlink)

    with pytest.raises(FileExistsError) as excinfo:
        migrate_music_manifest(tmp_path)
    message = str(excinfo.value)
    assert state["done"]
    assert str(legacy) in message
    assert str(current) in message
    # The name is still the symlink the other writer left, so nothing was
    # adopted as a completed hardlink and nothing was deleted.
    assert entry.is_symlink()


def test_a_name_rebound_to_a_different_file_before_the_open_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a name is re-bound between the lstat and the open [then] fail, [else stop]."""
    _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)
    legacy = tmp_path / LEGACY_MUSIC_MANIFEST_REL
    current = tmp_path / MUSIC_MANIFEST_REL
    fresh = '{"tracks": {"fresh": {}}}'
    state = _swap_at_open(monkeypatch, legacy, lambda: _atomic_write(legacy, fresh))

    with pytest.raises(FileExistsError) as excinfo:
        migrate_music_manifest(tmp_path)
    message = str(excinfo.value)
    assert state["done"]
    assert str(legacy) in message
    assert str(current) in message
    assert "replaced by another writer between the entry check and the open" in message
    # The writer's file survives under the legacy name, and no link was made.
    assert legacy.read_text(encoding="utf-8") == fresh
    assert not current.exists()


def test_concurrent_create_of_the_current_name_is_not_clobbered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a current manifest appears mid-migration and is replaced [then] fail, [else stop]."""
    _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)
    current = tmp_path / MUSIC_MANIFEST_REL
    concurrent = '{"tracks": {"fresh": {}}}'
    real_link = os.link

    def link_after_a_concurrent_write(*args: Any, **kwargs: Any) -> None:
        # Another writer lands the current name between migrate's legacy probe
        # and the syscall that moves it: exactly the window Sol's thread names.
        current.write_text(concurrent, encoding="utf-8", newline="\n")
        real_link(*args, **kwargs)

    monkeypatch.setattr(backup_manifest.os, "link", link_after_a_concurrent_write)

    with pytest.raises(FileExistsError) as excinfo:
        migrate_music_manifest(tmp_path)
    message = str(excinfo.value)
    assert str(tmp_path / LEGACY_MUSIC_MANIFEST_REL) in message
    assert str(current) in message
    assert current.read_text(encoding="utf-8") == concurrent
    assert (tmp_path / LEGACY_MUSIC_MANIFEST_REL).read_text(encoding="utf-8") == PAYLOAD


def _atomic_write(path: Path, text: str) -> None:
    """Simulate an adversarial atomic-replace writer in the migration's race tests.

    This helper is NOT what the shipped backup writer does. It is deliberately
    harder: a staging file plus ``os.replace`` splits ``path`` from the inode
    the migration hardlinked, which is the race these tests must refuse.

    The real writer, ``scripts/backup_music_to_bifrost2._write_manifest``, calls
    ``MANIFEST_PATH.write_text(...)``: open-with-truncate on the same inode. That
    keeps the retained hardlink intact, so after an ordinary backup run the
    legacy and current names are still one file and the next
    ``migrate_music_manifest`` is a clean no-op.
    """
    staging = path.with_name(path.name + ".staging")
    staging.write_text(text, encoding="utf-8", newline="\n")
    os.replace(staging, path)


def _replace_legacy_after_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh: str) -> None:
    """Make another writer replace the legacy NAME inside the link->check window."""
    legacy = tmp_path / LEGACY_MUSIC_MANIFEST_REL
    real_link = os.link

    def link_then_the_writer_lands(*args: Any, **kwargs: Any) -> None:
        real_link(*args, **kwargs)
        # An atomic-replace writer (the simulated worst case, not the shipped
        # ``_write_manifest``, which truncates in place) lands after migrate
        # linked the file it opened and before migrate finishes: the window
        # Sol's P1 names.
        _atomic_write(legacy, fresh)

    monkeypatch.setattr(backup_manifest.os, "link", link_then_the_writer_lands)


def test_legacy_replaced_mid_migration_is_refused_and_the_writer_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the migration deletes a legacy name it did not link [then] fail, [else stop]."""
    _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)
    legacy = tmp_path / LEGACY_MUSIC_MANIFEST_REL
    current = tmp_path / MUSIC_MANIFEST_REL
    fresh = '{"tracks": {"fresh": {}}}'
    _replace_legacy_after_link(tmp_path, monkeypatch, fresh)

    with pytest.raises(FileExistsError) as excinfo:
        migrate_music_manifest(tmp_path)
    message = str(excinfo.value)
    assert str(legacy) in message
    assert str(current) in message
    # Nothing is deleted: the writer's manifest survives under the legacy name,
    # and the refusal says which file the current name is holding.
    assert legacy.read_text(encoding="utf-8") == fresh
    assert current.read_text(encoding="utf-8") == PAYLOAD

    # Two different files is the documented refusal, not a guess: the migration
    # stops every time until the user deletes the stale one, and then migrates
    # the writer's manifest.
    with pytest.raises(FileExistsError):
        migrate_music_manifest(tmp_path)
    monkeypatch.undo()
    current.unlink()
    assert migrate_music_manifest(tmp_path) == current
    assert current.read_text(encoding="utf-8") == fresh
    assert os.stat(legacy).st_ino == os.stat(current).st_ino


def test_the_pre_link_handle_outlives_the_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the pre-link handle is closed before verification [then] fail, [else stop]."""
    _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)
    closed_early: list[bool] = []
    verified = False
    real_open, real_close = os.open, os.close
    real_verify = backup_manifest._verify_one_file

    def verify(*args: Any, **kwargs: Any) -> None:
        nonlocal verified
        verified = True
        real_verify(*args, **kwargs)

    def counting_open(*args: Any, **kwargs: Any) -> int:
        return real_open(*args, **kwargs)

    def counting_close(fd: int) -> None:
        closed_early.append(not verified)
        real_close(fd)

    monkeypatch.setattr(backup_manifest, "_verify_one_file", verify)
    monkeypatch.setattr(backup_manifest.os, "open", counting_open)
    monkeypatch.setattr(backup_manifest.os, "close", counting_close)

    migrate_music_manifest(tmp_path)

    # Holding the pre-link descriptor open through verification is what stops
    # the inode being freed and recycled onto a replacement that would then
    # compare equal to it.
    assert closed_early and not any(closed_early)


def test_legacy_replaced_before_the_link_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a legacy name rebound before the link is adopted as migrated [then] fail."""
    _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)
    legacy = tmp_path / LEGACY_MUSIC_MANIFEST_REL
    current = tmp_path / MUSIC_MANIFEST_REL
    replacement = '{"tracks": {"replacement": {}}}'
    real_link = os.link

    def writer_lands_then_link(*args: Any, **kwargs: Any) -> None:
        # Lands between migrate's pre-link pin and the link syscall: the link
        # can only reach the replacement, so the two names would agree with
        # each other while disagreeing with the file the migration opened.
        _atomic_write(legacy, replacement)
        real_link(*args, **kwargs)

    monkeypatch.setattr(backup_manifest.os, "link", writer_lands_then_link)

    with pytest.raises(FileExistsError) as excinfo:
        migrate_music_manifest(tmp_path)
    message = str(excinfo.value)
    assert str(legacy) in message
    assert str(current) in message
    # The writer's replacement is intact and nothing was deleted.
    assert legacy.read_text(encoding="utf-8") == replacement
    assert current.read_text(encoding="utf-8") == replacement


def test_legacy_removed_before_the_link_is_the_documented_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a legacy name removed before the link surfaces as an OSError [then] fail."""
    _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)
    legacy = tmp_path / LEGACY_MUSIC_MANIFEST_REL
    current = tmp_path / MUSIC_MANIFEST_REL
    real_link = os.link

    def writer_removes_then_link(*args: Any, **kwargs: Any) -> None:
        legacy.unlink()
        real_link(*args, **kwargs)

    monkeypatch.setattr(backup_manifest.os, "link", writer_removes_then_link)

    with pytest.raises(FileExistsError) as excinfo:
        migrate_music_manifest(tmp_path)
    message = str(excinfo.value)
    assert str(legacy) in message
    assert str(current) in message
    assert not current.exists()


def test_current_replaced_after_the_link_is_refused_not_reported_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] success is reported while the two names are different files [then] fail."""
    _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)
    legacy = tmp_path / LEGACY_MUSIC_MANIFEST_REL
    current = tmp_path / MUSIC_MANIFEST_REL
    fresh = '{"tracks": {"fresh": {}}}'
    real_link = os.link

    def link_then_the_writer_lands_current(*args: Any, **kwargs: Any) -> None:
        real_link(*args, **kwargs)
        # Migrate has linked the file it opened; another writer then atomically
        # replaces the CURRENT name. The legacy name is untouched, so a check
        # that only re-reads the legacy path sees nothing wrong and would
        # report "both names are one file" for two different files.
        _atomic_write(current, fresh)

    monkeypatch.setattr(backup_manifest.os, "link", link_then_the_writer_lands_current)

    with pytest.raises(FileExistsError) as excinfo:
        migrate_music_manifest(tmp_path)
    message = str(excinfo.value)
    assert str(legacy) in message
    assert str(current) in message
    # Nothing is deleted or replaced by the migration: the writer keeps its
    # file under the current name and the legacy bytes are still there.
    assert current.read_text(encoding="utf-8") == fresh
    assert legacy.read_text(encoding="utf-8") == PAYLOAD

    # Rerun keeps refusing, because the two names really are different files.
    with pytest.raises(FileExistsError):
        migrate_music_manifest(tmp_path)
    # Neither refusal may print the outcome of a migration it did not finish.
    assert "linked " not in capsys.readouterr().out


def test_current_removed_after_the_link_is_refused_not_reported_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a current name that is gone is reported as migrated [then] fail."""
    _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)
    legacy = tmp_path / LEGACY_MUSIC_MANIFEST_REL
    current = tmp_path / MUSIC_MANIFEST_REL
    real_link = os.link

    def link_then_remove_current(*args: Any, **kwargs: Any) -> None:
        real_link(*args, **kwargs)
        current.unlink()

    monkeypatch.setattr(backup_manifest.os, "link", link_then_remove_current)

    # There is no pair of names left to confirm, so no success can be claimed;
    # the legacy bytes are untouched because the migration removes nothing.
    with pytest.raises(FileExistsError) as excinfo:
        migrate_music_manifest(tmp_path)
    message = str(excinfo.value)
    assert str(legacy) in message
    assert str(current) in message
    assert not current.exists()
    assert legacy.read_text(encoding="utf-8") == PAYLOAD
    # A migration whose pair of names is gone may not print its success line.
    assert "linked " not in capsys.readouterr().out


def test_legacy_removed_mid_migration_keeps_the_bytes_it_linked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the legacy name vanishes mid-migration and its bytes are lost [then] fail, [else stop]."""
    _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)
    legacy = tmp_path / LEGACY_MUSIC_MANIFEST_REL
    current = tmp_path / MUSIC_MANIFEST_REL
    real_link = os.link

    def link_then_the_writer_deletes(*args: Any, **kwargs: Any) -> None:
        real_link(*args, **kwargs)
        legacy.unlink()

    monkeypatch.setattr(backup_manifest.os, "link", link_then_the_writer_deletes)

    # Nothing to unlink and nothing to refuse: the link is the only copy left.
    assert migrate_music_manifest(tmp_path) == current
    assert current.read_text(encoding="utf-8") == PAYLOAD
    assert not legacy.exists()


@pytest.mark.parametrize("current_present", [False, True], ids=["legacy-only", "both"])
def test_filesystem_without_hardlinks_is_refused_not_renamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current_present: bool
) -> None:
    """[if] a filesystem cannot hardlink and a rename is used instead [then] fail, [else stop]."""
    _write(tmp_path / LEGACY_MUSIC_MANIFEST_REL, PAYLOAD)
    current = tmp_path / MUSIC_MANIFEST_REL
    if current_present:
        _write(current, "{}")

    def unsupported(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.EOPNOTSUPP, os.strerror(errno.EOPNOTSUPP))

    monkeypatch.setattr(backup_manifest.os, "link", unsupported)

    with pytest.raises(OSError) as excinfo:
        migrate_music_manifest(tmp_path)
    assert "hardlink" in str(excinfo.value)
    assert (tmp_path / LEGACY_MUSIC_MANIFEST_REL).read_text(encoding="utf-8") == PAYLOAD
    if current_present:
        assert current.read_text(encoding="utf-8") == "{}"
    else:
        assert not current.exists()


@pytest.mark.parametrize("current_present", [False, True], ids=["neither", "current-only"])
def test_no_legacy_manifest_is_a_no_op(tmp_path: Path, current_present: bool) -> None:
    """[if] a checkout without the legacy file is changed [then] fail, [else stop]."""
    if current_present:
        _write(tmp_path / MUSIC_MANIFEST_REL, PAYLOAD)
    assert migrate_music_manifest(tmp_path) == tmp_path / MUSIC_MANIFEST_REL
    assert (tmp_path / MUSIC_MANIFEST_REL).exists() is current_present
    assert not (tmp_path / LEGACY_MUSIC_MANIFEST_REL).exists()


def test_a_symlinked_current_with_no_legacy_name_is_left_alone(tmp_path: Path) -> None:
    """[if] a checkout whose only manifest is a symlink is treated as migrated [then] fail, [else stop].

    Nothing is migrated in this checkout, so nothing is claimed about the entry:
    the reader through the link keeps working exactly as before.
    """
    real = tmp_path / "elsewhere" / "manifest.json"
    _write(real, PAYLOAD)
    current = tmp_path / MUSIC_MANIFEST_REL
    _symlink_or_skip(os.path.join("..", "..", "elsewhere", "manifest.json"), current)

    assert migrate_music_manifest(tmp_path) == current
    assert current.is_symlink()
    assert current.read_text(encoding="utf-8") == PAYLOAD
    assert not (tmp_path / LEGACY_MUSIC_MANIFEST_REL).exists()


def _run_fresh(code: str, store_host: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != B2STORE_SSH_HOST_ENV}
    if store_host is not None:
        env[B2STORE_SSH_HOST_ENV] = store_host
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return proc.stdout.strip().splitlines()[-1]


def test_writer_and_reader_share_the_manifest_path() -> None:
    """[if] backup_music_to_bifrost2 and backup_evicted_to_usb disagree on the path [then] fail, [else stop]."""
    code = (
        "import json\n"
        "from scripts import backup_evicted_to_usb as r, backup_music_to_bifrost2 as w\n"
        "print(json.dumps([str(w.MANIFEST_PATH), str(r.MANIFEST)]))\n"
    )
    writer, reader = json.loads(_run_fresh(code, None))
    expected = str(REPO_ROOT / MUSIC_MANIFEST_REL)
    assert writer == reader == expected


@pytest.mark.parametrize(
    ("store_host", "expected"),
    [(None, "bifrost2"), ("", "bifrost2"), ("asset-store-test-host", "asset-store-test-host")],
    ids=["unset", "blank", "override"],
)
def test_listing_error_names_the_configured_store(store_host: str | None, expected: str) -> None:
    """[if] b2shell's parse error names a host other than the configured store [then] fail, [else stop]."""
    code = (
        "from scripts.b2shell import parse_sizes\n"
        "try:\n"
        "    parse_sizes('not a size row\\n')\n"
        "except ValueError as exc:\n"
        "    print(exc)\n"
    )
    assert _run_fresh(code, store_host) == f"unparseable find output from {expected}: 'not a size row'"
