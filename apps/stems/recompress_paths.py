"""Work, backup and swap-journal paths for the stem recompress pass (split from apps/stems/recompress.py)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from apps.stems.bundle_lock import (
    beside_stem_root,
    data_dir_of_bundle,
)
from apps.stems.bundle_lock import (
    fsync_dir as _fsync_dir,
)

#: Where a live swap records the bundle it is replacing and the backup it moves
#: the old one to, ``<data>/state/.stem-recompress-swap/<stem root>/<stable_id>.json``,
#: so a pass killed between its two renames can be repaired by the next one.
SWAP_JOURNAL_DIR_NAME = ".stem-recompress-swap"

#: Where a pass stages one bundle's snapshot and new parts:
#: ``<stem root's parent>/.stem-recompress-work/<stem root name>/``. Beside the
#: stem root, never inside it, so a cache-budget scan or upload sweep of the
#: root never counts or queues the workspace as a bundle (Codex on PR #6821),
#: and on the same volume, so the swap stays a rename.
WORK_DIR_NAME = ".stem-recompress-work"


def work_parent(root: Path) -> Path:
    """The directory a pass makes its per-bundle work directories in, for ``root``.

    Beside the RESOLVED root, so a stem root that is a symlink to another disk
    gets its workspace on that disk and the swap stays a same-volume rename
    (Codex on #6820 and #6821).
    """
    return beside_stem_root(root) / WORK_DIR_NAME / root.name


#: Where a ``--live`` pass with no ``--backup-dir`` keeps the old bundles.
BACKUP_DIR_NAME = "stems-recompress-backup"


def default_backup_root(root: Path, stamp: str) -> Path:
    """The default backup root for bundles of ``root``: beside the RESOLVED root,
    on its volume, so moving the old bundle aside is a rename there too.

    Namespaced by the resolved root's name: two data dirs whose stem roots
    link to sibling directories on one volume share the directory beside
    them, and passes started in the same second would otherwise share one
    backup path per stable id (Codex on #6820)."""
    real = Path(os.path.realpath(root))
    return real.parent / BACKUP_DIR_NAME / real.name / stamp


def swap_journal_path(bundle_dir: Path) -> Path:
    """The journal entry for one bundle's swap, keyed on the bundle alone."""
    return (
        data_dir_of_bundle(bundle_dir)
        / "state"
        / SWAP_JOURNAL_DIR_NAME
        / bundle_dir.parent.name
        / f"{bundle_dir.name}.json"
    )


def _write_swap_journal(bundle_dir: Path, backup_dir: Path) -> Path:
    journal = swap_journal_path(bundle_dir)
    _mkdir_durably(journal.parent)
    tmp = journal.with_suffix(".json.tmp")
    payload = json.dumps({"bundle": str(bundle_dir.resolve()), "backup": str(backup_dir.resolve())}) + "\n"
    # Durable before the caller's first rename: after a power loss mid-swap
    # the journal is the only record that the live bundle sits in the backup
    # (Codex on #6820). The file is flushed to disk, then the directory entry
    # the replace wrote.
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(journal)
    _fsync_dir(journal.parent)
    return journal


def _mkdir_durably(directory: Path) -> None:
    """``mkdir -p`` whose new entries reach the disk: each directory it
    creates is flushed into its parent, so the first swap under a stem root
    cannot lose the journal's own directories to a power loss (Codex on
    #6820). Directories that already exist are left as they are."""
    missing: list[Path] = []
    probe = directory
    while not probe.is_dir():
        missing.append(probe)
        probe = probe.parent
    for created in reversed(missing):
        created.mkdir(exist_ok=True)
        _fsync_dir(created.parent)


def orphaned_swaps(data_dir: Path) -> list[Path]:
    """Journal entries a pass killed mid-swap left behind (read-only)."""
    journal_root = data_dir / "state" / SWAP_JOURNAL_DIR_NAME
    if not journal_root.is_dir():
        return []
    return sorted(journal_root.glob("*/*.json"))
