"""Import the GCE stem farm's bundle tars into a stem root, under the bundle lock.

``scripts/stem_farm_watch_pull_down.sh`` pulls one ``<stable_id>.tar`` per
track from the farm VM's outbox and calls this. Each tar is unpacked into a
staging directory beside the stem root, never into the live bundle, and only
then swapped in under the bundle's cross-process lock
(``apps/stems/bundle_lock.py``): the old bundle moves aside, the new one is
renamed in, and only then is the old one deleted. Before the swap the
staged bundle must load with the strict reader
(``apps.stems.artifacts.load_stem_bundle``) under its own stable_id: the farm
runner writes its outbox tar in place, so a structurally valid tar can still
be missing parts or hold a bad manifest, and that must never replace a
working bundle (Codex P1 on #6821). Unpacking straight into ``<stems>/<stable_id>`` let a STEM-58
re-compress pass snapshot or move a half-extracted bundle, or the importer
keep writing into a directory the pass had already moved to its backup (Codex
P1 on #6821).

Usage: ``python -m apps.stems.farm_import <pull dir> <stem root>``, or
``python -m apps.stems.farm_import --preflight`` to check this interpreter
can unpack safely before anything is pulled.

-Claude
"""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
from pathlib import Path

from apps.stems.artifacts import load_stem_bundle
from apps.stems.bundle_lock import (
    beside_stem_root,
    bundle_lock,
    data_dir_of_bundle,
    remove_staging,
    replace_bundle_dir,
)

STAGING_DIR_NAME = ".stem-farm-import"


class UnsafeTarExtractionError(RuntimeError):
    """This interpreter has no tar ``data`` filter, so a farm tar cannot be unpacked safely."""


def require_tar_data_filter() -> None:
    """Refuse an interpreter without ``tarfile.data_filter`` (CPython before 3.11.4).

    ``requires-python`` admits 3.11.0 to 3.11.3, where ``extractall`` takes no
    ``filter`` and raised ``TypeError`` only after the pull, leaving the farm
    VM running (Codex on #6821). Unpacking without the filter would let a tar
    write outside the staging directory, so the import is refused instead,
    and the pull-down script asks this before it downloads anything.
    """
    if not hasattr(tarfile, "data_filter"):
        raise UnsafeTarExtractionError(
            f"Python {sys.version.split()[0]} has no tarfile data filter (needs 3.11.4 or later); "
            "refusing to unpack farm tars without it"
        )


def import_tar(tar_path: Path, stems: Path) -> Path:
    """Unpack one ``<stable_id>.tar`` and swap it in as ``stems/<stable_id>``."""
    require_tar_data_filter()
    dest = stems / tar_path.stem
    data_dir_of_bundle(dest)  # refuse a stem root with no shared lock, before writing anything
    # On the stem root's own volume, so the publishing rename never crosses
    # filesystems when ``stems`` is a symlink to another disk.
    staging_root = beside_stem_root(stems) / STAGING_DIR_NAME
    staging_root.mkdir(parents=True, exist_ok=True)
    # <staging root>/<pid>/<stable_id>, so the strict reader loads it under
    # the id it will be published as.
    staging_parent = staging_root / str(os.getpid())
    staging = staging_parent / tar_path.stem
    if staging_parent.exists():
        shutil.rmtree(staging_parent)
    try:
        staging.mkdir(parents=True)
        with tarfile.open(tar_path) as tar:
            tar.extractall(staging, filter="data")
        load_stem_bundle(tar_path.stem, roots=[staging_parent])
        with bundle_lock(dest):
            replace_bundle_dir(staging, dest)
    finally:
        remove_staging(staging_parent, "farm import staging directory")
    return dest


def import_all(pull: Path, stems: Path) -> int:
    """Import every ``*.tar`` in ``pull``; returns how many bundles landed."""
    stems.mkdir(parents=True, exist_ok=True)
    count = 0
    for tar_path in sorted(pull.glob("*.tar")):
        import_tar(tar_path, stems)
        count += 1
        print(f"unpacked {tar_path.stem}")
    print(f"imported {count} bundles -> {stems}")
    return count


def main(argv: list[str]) -> int:
    if argv == ["--preflight"]:
        try:
            require_tar_data_filter()
        except UnsafeTarExtractionError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        return 0
    if len(argv) != 2:
        print("usage: python -m apps.stems.farm_import <pull dir> <stem root>", file=sys.stderr)
        return 2
    import_all(Path(argv[0]), Path(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
