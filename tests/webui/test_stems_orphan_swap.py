"""[if] a swap was interrupted between its two renames [then] the next deck read puts the bundle back (Codex on #6820), [else stop]."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI

from apps.stems.bundle_lock import bundle_lock, swap_backup_path
from tests.webui.test_stems_hydration import _client
from tests.webui.test_stems_waveform_pins import SID, _layout

pytestmark = pytest.mark.requirement("STEM-58")


def _get_waveform(stems_dir: Path, data_dir: Path) -> int:
    with _client(stems_dir, data_dir=data_dir) as client:
        assert isinstance(client.app, FastAPI)
        client.app.state.stem_cache_data_dir = data_dir
        client.app.state.data_dir = data_dir
        return client.get(f"/api/v1/tracks/{SID}/stems/vocals/waveform").status_code


def test_a_read_restores_a_bundle_a_killed_swap_left_at_its_backup(tmp_path: Path) -> None:
    """[if] a writer died after moving the bundle aside [then] the next read puts it back and serves it, [else stop].

    The first rename of a real swap is done for real (``dest`` to its swap
    backup) and the second never happens, as when the publisher is killed
    between them. Control: with a bundle still at ``dest``, a leftover backup
    is not touched by a read; discarding it is the next writer's job.

    MUTATION TARGET: drop the ``restore_orphaned_swap`` call in the route's
    read barrier.
    """
    data_dir, stems_dir = _layout(tmp_path)
    dest = stems_dir / SID
    backup = swap_backup_path(dest)
    backup.parent.mkdir(parents=True)
    dest.rename(backup)
    assert not dest.exists()

    assert _get_waveform(stems_dir, data_dir) == 200
    assert (dest / "manifest.json").is_file(), "the bundle is back at its canonical path"
    assert not backup.exists()

    backup.mkdir()
    (backup / "manifest.json").write_bytes(b"stale")
    assert _get_waveform(stems_dir, data_dir) == 200
    assert (backup / "manifest.json").read_bytes() == b"stale", (
        "control: a read never touches a backup beside a live bundle"
    )


def test_a_read_restores_a_killed_swap_under_a_symlinked_stem_root(tmp_path: Path) -> None:
    """[if] the stem root is a symlink to another volume [then] a read still restores the orphaned bundle, [else stop].

    The resolved root lies outside ``<data>/state``, so recovery keyed on it
    refused the bundle and looked for its backup in the wrong place (Codex on
    #6820). The backup sits where a real swap of the lexical path puts it,
    beside the resolved root. Control: the backup really is outside the data
    dir, so the read could not have found it by the lexical parent alone.

    MUTATION TARGET: resolve the root before ``restore_orphaned_swap``.
    """
    data_dir, stems_dir = _layout(tmp_path)
    volume = tmp_path / "volume" / "stems"
    volume.parent.mkdir()
    stems_dir.rename(volume)
    stems_dir.symlink_to(volume, target_is_directory=True)
    dest = stems_dir / SID
    backup = swap_backup_path(dest)
    assert not backup.is_relative_to(data_dir), "control: the backup lives on the stem root's volume"
    backup.parent.mkdir(parents=True)
    dest.rename(backup)
    assert not dest.exists()

    assert _get_waveform(stems_dir, data_dir) == 200
    assert (volume / SID / "manifest.json").is_file(), "the bundle is back at its canonical path"
    assert not backup.exists()


def test_a_read_restores_a_bundle_a_killed_recompression_left_in_its_backup(tmp_path: Path) -> None:
    """[if] a recompression died between its two renames [then] the next read puts the bundle back from its journal, [else stop].

    A recompression keeps the old bundle at a timestamped backup that only
    its journal under ``.stem-recompress-swap`` records, not at the swap
    backup the generic recovery looks for, so the track stayed missing until
    the next ``recompress --live`` (Codex on #6820). The journal is the real
    one a pass writes, and the first rename is done for real. Control: with a
    bundle in place, a read leaves the journal and its backup alone; clearing
    them is the next pass's job.

    MUTATION TARGET: drop the ``restore_orphaned_recompress_swap`` call in
    the route's read barrier.
    """
    from apps.stems.recompress import _write_swap_journal, swap_journal_path

    data_dir, stems_dir = _layout(tmp_path)
    dest = stems_dir / SID
    backup = tmp_path / "stems-recompress-backup" / "20261017T075100" / SID
    backup.parent.mkdir(parents=True)
    journal = _write_swap_journal(dest, backup)
    assert journal == swap_journal_path(dest)
    dest.rename(backup)
    assert not swap_backup_path(dest).exists(), "control: the generic swap backup is not where this one is"

    assert _get_waveform(stems_dir, data_dir) == 200
    assert (dest / "manifest.json").is_file(), "the bundle is back at its canonical path"
    assert not backup.exists()
    assert not journal.exists(), "the entry goes once the bundle is back"

    backup.mkdir()
    (backup / "manifest.json").write_bytes(b"old")
    journal = _write_swap_journal(dest, backup)
    assert _get_waveform(stems_dir, data_dir) == 200
    assert journal.is_file() and (backup / "manifest.json").read_bytes() == b"old", (
        "control: a read never touches a journal or backup beside a live bundle"
    )


def test_a_read_restores_an_orphaned_swap_only_once_the_writer_holding_the_bundle_lock_lets_go(
    tmp_path: Path,
) -> None:
    """[if] a publisher holds the bundle lock while a read finds a dead swap's backup [then] the restore waits for it, [else stop].

    The restore is a rename into the live path. Taken under the read barrier
    alone it landed while a publisher that had already seen no bundle there
    held the bundle lock, so that publish replaced the restored bundle
    without carrying its files over (Sol on #6820). Control: once the lock is
    released the read restores and serves the bundle.

    MUTATION TARGET: restore under ``read_barrier`` only, without the bundle
    lock.
    """
    data_dir, stems_dir = _layout(tmp_path)
    dest = stems_dir / SID
    backup = swap_backup_path(dest)
    backup.parent.mkdir(parents=True)
    dest.rename(backup)
    status: list[int] = []

    with bundle_lock(dest):
        reader = threading.Thread(target=lambda: status.append(_get_waveform(stems_dir, data_dir)))
        reader.start()
        time.sleep(0.5)
        assert not dest.exists() and backup.is_dir(), "nothing is restored while a writer holds the bundle lock"
    reader.join(30)

    assert status == [200]
    assert (dest / "manifest.json").is_file()
    assert not backup.exists()


def test_a_read_of_a_bundle_being_hydrated_never_waits_on_the_hydration_lock(tmp_path: Path) -> None:
    """[if] no dead swap left anything for a bundle a hydration holds locked [then] the read answers at once, [else stop].

    The restore takes the bundle lock only when a swap backup or a
    recompression journal is on disk: a missing bundle is usually one a
    hydration is fetching under that lock, and the poll must not wait on its
    network fetch (Codex P1 on #6821). Control: the bundle really is under
    ``<data>/state``, where the restore path is armed.

    MUTATION TARGET: drop the leftovers check, so every read of a missing
    shared-store bundle takes the bundle lock.
    """
    import shutil

    data_dir, stems_dir = _layout(tmp_path)
    dest = stems_dir / SID
    shutil.rmtree(dest)
    assert dest.parent.parent.name == "state", "control: the restore path is armed for this bundle"
    status: list[int] = []

    with bundle_lock(dest):
        reader = threading.Thread(target=lambda: status.append(_get_waveform(stems_dir, data_dir)))
        reader.start()
        reader.join(10)
        assert status, "the read waited on the hydration's bundle lock"
    reader.join(30)
    assert status[0] != 200 and not dest.exists()
