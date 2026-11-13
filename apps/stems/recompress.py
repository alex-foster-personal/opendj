"""Re-compress existing lossless stem bundles of lossy sources to MP3 (STEM-58),
and shrink MP3 bundles made under the old per-part rule to STEM-8231's.

STEM-8231 (Mon 2 Nov / Tue 3 Nov 2026): an MP3 bundle whose parts are over the
whole-set rule (each part at max(floor(source_kbps / 4) on the ladder, its
floor: 96 kbps bass and drums, 64 vocals and other), measured as part bytes
over the stems' duration, is decoded and re-encoded to that rule through the
same encoder; a compliant one is skipped as SKIP_AT_POLICY. Everything below
(locking, snapshot, verify, backup, journaled swap) applies to both.

WHY. From Fri 2 Oct 2026 (STEM-51) until STEM-57, the installed app wrote every
stem of an MP3/M4A source as lossless FLAC, and the Modal/relay routes have
always written FLAC (farm schema v2). Measured on a real 60 s mix, four FLAC
parts total 18.7 MB against a 1.4-2.4 MB source: each part 2.5x to 4.2x the
source. This pass brings those bundles to the size policy a new separation
follows (apps/stems/stem_size_policy.py): a lossy source gets MP3 parts at or
under the source bitrate, through the same encoder and ladder
(apps.stems.stem_encode.encode_part).

WHAT IT TOUCHES. Bundle directories under this machine's stem roots
(``<data>/state/stems`` and the RoFormer root), and only those whose parts are
FLAC or WAV while their source is MP3/M4A. A lossless source keeps FLAC; a
bundle that is already MP3 is left alone. R2 copies are not touched by this pass: re-publishing is a separate,
deliberate step. A live swap updates the local download index through
:func:`apps.cloud.stem_index_sync.record_converted_bundle`. An R2 hydrate
also calls reencode_staged_bundle on its private verify copy before
publishing (#9665).

SAFETY.
  * Dry run by default: it prints what it would do and writes nothing.
  * ``--live`` writes the new bundle beside the old one, loads it with the real
    reader (apps.stems.artifacts.load_stem_bundle) and checks every part's
    decoded frame count, and only then swaps it in.
  * The old bundle is MOVED to a timestamped backup directory, never deleted.
    Deleting the backup, after a listen, is the operator's call. The move is a
    rename on the same volume, journaled under
    ``<data>/state/.stem-recompress-swap/`` until the new bundle is in place,
    so a pass killed between the two renames is repaired by the next
    ``--live`` run before it plans (the backup goes back).
  * It refuses while a stems job is queued or running in ``jobs.db``, and when
    ``jobs.db`` cannot be read (absence of evidence is not idle;
    (private path)). ``--no-jobs-db-ok`` accepts a data dir that
    has never had a jobs database. The same check runs again immediately
    before EACH bundle is swapped in: a job that appeared mid-pass stops that
    swap and the rest of the pass (exit 3), leaving those bundles as they
    were.
  * Every writer of a bundle directory holds its cross-process lock
    (apps/stems/bundle_lock.py): a stems job's publish, an R2 hydrate, and
    this pass, which holds it to snapshot a bundle and again for the job
    re-check and the swap, never for the encode. Under that lock the bundle's
    content fingerprint must still match the snapshot; a bundle another writer
    replaced mid-encode is left as that writer published it and counted as a
    failure, never clobbered or moved into the backup.
  * A lossless bundle whose manifest says FLAC was chosen on purpose (a FLAC
    tier's preset, or an explicit codec from a policy-aware writer) is left
    alone; see deliberate_lossless_reason.
  * Each bundle is staged in a work directory beside the stem root
    (``<root parent>/.stem-recompress-work/<root name>/``), never inside it, so
    a concurrent cache-budget scan never counts or evicts against it. A work
    directory that cannot be removed is an error naming the directory, never
    a silent leftover.
  * A part's source is the library's current file for that stable_id
    (state.db), else the manifest's recorded source path; with neither on disk
    the bundle is skipped and counted, because the bitrate ladder needs the
    source's real size.

Run (on the machine that holds the stems):

    uv run --extra analysis python -m apps.stems.recompress            # dry run
    uv run --extra analysis python -m apps.stems.recompress --live --limit 5
    uv run --extra analysis python -m apps.stems.recompress --live
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from apps.cloud.stem_index_sync import record_converted_bundle
from apps.shared.hashing import sha256_file_hex
from apps.stems.artifacts import (
    load_stem_bundle,
)
from apps.stems.bundle_lock import (
    SwapBackupStuckError,
    _rename_durably,
    bundle_lock,
    data_dir_of_bundle,
    discard_landed_swap_backup,
    readers_held_off,
    swap_backup_path,
    undo_failed_swap,
)
from apps.stems.bundle_lock import (
    fsync_dir as _fsync_dir,
)
from apps.stems.bundle_lock import (
    fsync_tree as _fsync_tree,
)
from apps.stems.recompress_classify import *  # noqa: F403
from apps.stems.recompress_classify import (
    ELIGIBLE,
    LIVE_JOB_STATUSES,
    BundleChangedError,
    Candidate,
    Outcome,
    StemsJobLiveError,
    WorkVolumeError,
    _dir_bytes,
    _raw_manifest,
    _source_duration_s,
    library_paths,
    readonly_sqlite_uri,
    stem_roots_for,
)
from apps.stems.recompress_encode import *  # noqa: F403
from apps.stems.recompress_encode import (
    _remove_work,
    _require_still_eligible,
    _source_signature,
    _write_mp3_bundle,
)
from apps.stems.recompress_paths import *  # noqa: F403
from apps.stems.recompress_paths import (
    _mkdir_durably,
    _write_swap_journal,
    orphaned_swaps,
    swap_journal_path,
    work_parent,
)
from apps.stems.recompress_snapshot import *  # noqa: F403
from apps.stems.recompress_snapshot import (
    _marks,
    _require_recorded_source,
    bundle_fingerprint,
)

# ----- the live-job guard ----------------------------------------------------


def assert_no_live_stems_job(data_dir: Path, *, missing_ok: bool) -> None:
    """Refuse while any stems job is queued or running (fail closed)."""
    jobs_db = data_dir / "state" / "jobs.db"
    if not jobs_db.is_file():
        if missing_ok:
            return
        raise StemsJobLiveError(
            f"{jobs_db} is missing, so a live stems job cannot be ruled out; "
            "pass --no-jobs-db-ok only for a data dir that has never run jobs"
        )
    try:
        connection = sqlite3.connect(readonly_sqlite_uri(jobs_db.resolve()), uri=True)
        try:
            placeholders = ",".join("?" for _ in LIVE_JOB_STATUSES)
            rows = connection.execute(
                f"SELECT id, kind, status FROM jobs WHERE kind LIKE 'stems.%' AND status IN ({placeholders})",
                LIVE_JOB_STATUSES,
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise StemsJobLiveError(f"cannot read {jobs_db}: {exc}") from exc
    if rows:
        listed = ", ".join(f"{kind} {status} ({job_id})" for job_id, kind, status in rows[:5])
        raise StemsJobLiveError(f"{len(rows)} stems job(s) live: {listed}")


# ----- one bundle --------------------------------------------------------------


def _swap_in(
    new_dir: Path,
    bundle_dir: Path,
    backup_dir: Path,
    read_fingerprint: str,
    before_swap: Callable[[], None] | None,
    source_unchanged: Callable[[], None],
    *,
    source_bytes_unchanged: Callable[[], None] | None = None,
    source_path: Path | None = None,
) -> int:
    """Move ``bundle_dir`` to ``backup_dir`` and ``new_dir`` into its place.

    The content hashes (the bundle's :func:`bundle_fingerprint`, then
    ``source_bytes_unchanged``, the source file's SHA-256) read every byte of
    a long mix and its stems, so they run outside the striped bundle lock,
    which a hydrate or publish of any id on that stripe would otherwise wait
    out (Codex on #6821). Under the lock first, the bundle's and the
    source's :func:`_marks` are recorded; then the hashes; then, under the
    lock again: ``before_swap`` (the live-job re-check), ``source_unchanged``
    (the library path and source signature), the marks compared, and the two
    renames, so no other writer of this id can publish between the checks
    and the move. Returns the old bundle's bytes.
    """
    with bundle_lock(bundle_dir):
        marks = (_marks(bundle_dir), _marks(source_path))
    try:
        unlocked_fingerprint = bundle_fingerprint(bundle_dir)
    except (FileNotFoundError, NotADirectoryError) as exc:  # a writer moved it mid-hash
        raise BundleChangedError(
            f"{bundle_dir.name}: replaced while this pass hashed it; left as the other writer published it"
        ) from exc
    if unlocked_fingerprint != read_fingerprint:
        raise BundleChangedError(
            f"{bundle_dir.name}: rewritten by another writer while this pass encoded it; "
            "left as that writer published it"
        )
    # The source last, right before the locked re-check: on Windows its
    # change time is the creation time, so a same-size rewrite that puts
    # its mtime back is visible only to this hash, and a rewrite during the
    # long bundle hash must not land after it (Codex P1 on #6820).
    if source_bytes_unchanged is not None:
        source_bytes_unchanged()
    with bundle_lock(bundle_dir):
        if before_swap is not None:
            before_swap()
        source_unchanged()
        if marks[0] is None or (_marks(bundle_dir), _marks(source_path)) != marks:
            raise BundleChangedError(
                f"{bundle_dir.name}: replaced or rewritten while this pass verified it; "
                "left as the other writer published it"
            )
        # A generic swap backup a publisher killed after landing this bundle
        # left behind is an older render. Were this pass killed between its
        # renames, the read barrier would restore that backup before this
        # pass's own journal and serve it, stranding the newer bundle in
        # ``backup_dir``; so it goes before anything moves (Codex on #6820).
        if not discard_landed_swap_backup(bundle_dir):
            raise SwapBackupStuckError(swap_backup_path(bundle_dir))
        before = _dir_bytes(bundle_dir)
        _mkdir_durably(backup_dir.parent)
        if backup_dir.exists():
            raise FileExistsError(f"backup already holds {backup_dir}")
        # The new bundle's bytes reach the disk before anything moves: renames
        # flush only directory entries, so without this a power loss after the
        # journal is gone could leave the live bundle's parts empty and no
        # record of the intact backup (Codex on #6820).
        _fsync_tree(new_dir)
        journal = _write_swap_journal(bundle_dir, backup_dir)
        # Plain renames, never a copy-then-delete: each step is atomic, so a
        # process killed between them leaves one whole bundle in exactly one
        # of the two places the journal names. A --backup-dir on another
        # volume fails here (EXDEV) before anything has moved. Each rename is
        # flushed into both directories before the next step, and the journal
        # goes only once the bundle's final place is on disk, so after a power
        # loss the journal never outlives the moves it describes (Codex P1 on
        # #6821).
        # The canonical path is empty between the two renames, so readers are
        # held off for the window (Codex on #6821).
        # A flush can raise after its rename landed, so a failure anywhere in
        # the two moves is undone from what is on disk, and the journal goes
        # only once the old bundle is back (Sol on #6820).
        with readers_held_off(bundle_dir):
            try:
                _rename_durably(bundle_dir, backup_dir)
                _rename_durably(new_dir, bundle_dir)
            except BaseException:
                undo_failed_swap(bundle_dir, backup_dir, new_dir)
                _remove_journal(journal)
                raise
        # The swap has landed: readers already get the new bundle, so a
        # journal entry that will not go is not this bundle's failure. With
        # the bundle in place the entry reads as landed to every later reader
        # (recover_orphaned_swaps, clear_landed_recompress_journal), so it is
        # left for the next pass and named here (Codex on #6820).
        try:
            _remove_journal(journal)
        except OSError as exc:
            print(
                f"{bundle_dir.name}: swapped in, but its journal entry {journal} was not cleared ({exc}); "
                "the next --live pass clears it",
                file=sys.stderr,
            )
    return before


def _remove_journal(journal: Path) -> None:
    """Remove a swap journal entry and flush its directory."""
    journal.unlink()
    _fsync_dir(journal.parent)


def recover_orphaned_swaps(data_dir: Path) -> list[str]:
    """Repair every swap a killed pass left half done, before anything is planned.

    Under the bundle lock, for each journal entry: no bundle in place but the
    backup whole means the pass died between its two renames, so the backup
    is the only good copy and goes back (Codex on #6820); a bundle in place
    means the swap landed, or never started, and the backup (if any) is the
    operator's kept copy, so only the entry goes. Neither on disk is reported
    and the entry is kept, never guessed at. Returns one line per entry.
    """
    stem_roots = {root.name: root for root in stem_roots_for(data_dir)}
    lines: list[str] = []
    for journal in orphaned_swaps(data_dir):
        root = stem_roots.get(journal.parent.name)
        if root is None:
            raise BundleChangedError(f"{journal}: names no stem root of {data_dir}; left for the operator")
        bundle_dir = root / journal.stem
        # Read the entry under the lock (Codex on #6820): a pass still mid-swap
        # holds it, and by the time it lets go it has removed its own entry.
        # The read barrier too, in the order a live swap takes them, so a deck
        # read restoring the same entry (restore_orphaned_recompress_swap)
        # waits for this one rather than racing it (Codex on #6820).
        with bundle_lock(bundle_dir), readers_held_off(bundle_dir):
            if not journal.exists():
                lines.append(f"{bundle_dir.name}: the pass that wrote this entry finished it")
                continue
            recorded = json.loads(journal.read_text(encoding="utf-8"))
            if Path(recorded["bundle"]) != bundle_dir.resolve():
                raise BundleChangedError(
                    f"{journal}: records {recorded['bundle']}, not {bundle_dir}; left for the operator"
                )
            backup_dir = Path(recorded["backup"])
            if bundle_dir.exists():
                _remove_journal(journal)
                lines.append(f"{bundle_dir.name}: swap had landed; nothing to restore")
            elif backup_dir.is_dir():
                _rename_durably(backup_dir, bundle_dir)
                _remove_journal(journal)
                lines.append(f"{bundle_dir.name}: restored the old bundle from {backup_dir}")
            else:
                raise BundleChangedError(
                    f"{bundle_dir.name}: neither the bundle nor its backup {backup_dir} is on disk; "
                    f"journal {journal} left for the operator"
                )
    return lines


def restore_orphaned_recompress_swap(bundle_dir: Path, data_dir: Path) -> bool:
    """Put back the bundle a recompression killed between its two renames left in its backup.

    Called by a deck read while it holds the bundle lock and then
    :func:`readers_held_off`, in a swap's own order. A live pass holds both
    across its renames and removes its
    journal entry inside it when it rolls back, so under it a journal entry
    with no bundle in place can only be a dead pass's, and its backup is the
    only good copy. Without this the track read as missing until the next
    ``recompress --live`` ran :func:`recover_orphaned_swaps` (Codex on #6820).
    A bundle in place is left alone: that swap landed, or never started, and
    the next pass clears the entry. An entry naming another bundle, or a
    backup that is not on disk, is left for the operator. Returns whether a
    bundle was put back.
    """
    bundle_dir = Path(bundle_dir)
    # The bundle being there is the common case: one stat, nothing read.
    if bundle_dir.exists():
        return False
    try:
        if data_dir_of_bundle(bundle_dir).resolve() != Path(data_dir).resolve():
            return False
    except ValueError:
        return False
    journal = swap_journal_path(bundle_dir)
    if not journal.is_file():
        return False
    recorded = json.loads(journal.read_text(encoding="utf-8"))
    backup_dir = Path(recorded["backup"])
    if Path(recorded["bundle"]) != bundle_dir.resolve() or not backup_dir.is_dir():
        return False
    _rename_durably(backup_dir, bundle_dir)
    _remove_journal(journal)
    return True


def clear_landed_recompress_journal(bundle_dir: Path) -> None:
    """Remove the journal entry of a recompression that landed but died before clearing it.

    The caller holds ``bundle_dir``'s bundle lock and is about to remove the
    bundle (cache eviction). A pass killed after its second rename and
    before :func:`_remove_journal` leaves the new bundle, the old backup and
    the entry; once eviction removes the bundle, a deck read would take the
    entry for an interrupted swap and put the old bundle back
    (:func:`restore_orphaned_recompress_swap`), undoing the eviction and the
    recompression (Codex on #6820). With the bundle still in place the entry
    proves only that the swap landed, so it goes, as
    :func:`recover_orphaned_swaps` would remove it; the backup is the
    operator's kept copy and stays.
    """
    bundle_dir = Path(bundle_dir)
    if not bundle_dir.is_dir():
        return
    try:
        journal = swap_journal_path(bundle_dir)
    except ValueError:
        return
    if journal.is_file():
        _remove_journal(journal)


def _pin_generation(bundle_dir: Path, snapshot: Path) -> bool:
    """Hard-link every file of ``bundle_dir`` into ``snapshot``; False if the volume refuses.

    Held under the bundle lock, and cheap there: one link per file, no stem
    bytes read. Every writer of a bundle stages a new directory and renames
    it into place (:func:`apps.stems.bundle_lock.replace_bundle_dir`), and
    none rewrites a part in place, so the linked inodes keep this generation's
    bytes after another writer replaces or evicts the bundle. A volume
    without hard links (FAT, exFAT) leaves nothing behind and returns False.
    """
    snapshot.mkdir(parents=True)

    def refuse(exc: OSError) -> None:
        raise exc

    try:
        # A walk error (the bundle gone) raises, as a copy would have.
        for current, dirs, files in os.walk(bundle_dir, onerror=refuse):
            rel = Path(current).relative_to(bundle_dir)
            for name in dirs:
                (snapshot / rel / name).mkdir()
            for name in files:
                os.link(Path(current) / name, snapshot / rel / name)
    except (FileNotFoundError, NotADirectoryError):
        raise
    except OSError:
        shutil.rmtree(snapshot)
        return False
    return True


def _copy_unlocked(bundle_dir: Path, snapshot: Path) -> None:
    """Copy ``bundle_dir`` into ``snapshot`` without the bundle lock.

    For a volume that refuses hard links. A writer that replaces the bundle
    mid-copy leaves a copy mixing two generations, whose fingerprint matches
    neither, so the swap's re-hash of the live bundle refuses it
    (:func:`_swap_in`); one that removes it mid-copy stops this bundle here.
    """
    try:
        shutil.copytree(bundle_dir, snapshot)
    except (FileNotFoundError, NotADirectoryError, shutil.Error) as exc:
        raise BundleChangedError(
            f"{bundle_dir.name}: changed or unreadable while this pass copied it ({exc}); left as it is"
        ) from exc


def recompress_bundle(
    candidate: Candidate,
    backup_root: Path,
    *,
    before_swap: Callable[[], None] | None = None,
) -> Outcome:
    """Re-encode one eligible bundle to MP3, verify it, then swap it in.

    The old directory moves to ``backup_root/<root name>/<stable_id>``. On any
    failure before the swap nothing in the stem root has changed.

    ``before_swap`` runs after the new bundle is verified and immediately
    before the old one is moved; if it raises, nothing is swapped. The CLI
    passes the live-stems-job check here, so a job that appeared while this
    bundle was encoding stops its swap.
    """
    if candidate.verdict != ELIGIBLE or candidate.source_path is None:
        raise ValueError(f"{candidate.stable_id} is not eligible: {candidate.verdict}")
    root, stable_id = candidate.root, candidate.stable_id
    bundle_dir = root / stable_id
    work_root = work_parent(root)
    work_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".recompress-{stable_id}-", dir=str(work_root)))
    try:
        if work.stat().st_dev != root.stat().st_dev:
            raise WorkVolumeError(
                f"{stable_id}: work directory {work} is not on the stem root's volume ({root}); "
                "the swap is a rename, so it needs the same volume"
            )
        # Snapshot the bundle into the work dir and fingerprint the snapshot,
        # so the encode below (which runs without the lock) reads bytes no
        # other writer can rewrite mid-read, and the swap can tell whether
        # the live bundle changed since. Under the lock the generation is
        # only PINNED, by hard links: a copy of every stem byte held there
        # would make every hydrate or publish of an id on that stripe wait it
        # out (Codex on #6821).
        snapshot_root = work / "snapshot"
        snapshot = snapshot_root / stable_id
        with bundle_lock(bundle_dir):
            pinned = _pin_generation(bundle_dir, snapshot)
            library_path = library_paths(data_dir_of_bundle(bundle_dir), [stable_id]).get(stable_id)
        if not pinned:
            _copy_unlocked(bundle_dir, snapshot)
        # The plan is stale by now: another writer may have replaced the bundle,
        # or the library relinked the track, since (Codex on #6820). Decide
        # again from the locked copy and the library's current path.
        source_path = _require_still_eligible(snapshot_root, stable_id, candidate, library_path)
        read_fingerprint = bundle_fingerprint(snapshot)
        bundle = load_stem_bundle(stable_id, roots=[snapshot_root])
        old_raw = _raw_manifest(snapshot)
        source_digest = _require_recorded_source(stable_id, old_raw, source_path)
        source_signature = _source_signature(source_path)
        source_bytes = source_signature[0]
        # The source's own duration, not the stems': classify() refused one
        # that disagrees beyond the drift tolerance, and this is the length
        # the source's bytes actually cover (Sol on #6820).
        duration_s = _source_duration_s(source_path)
        if duration_s is None:
            raise BundleChangedError(f"{stable_id}: {source_path} no longer reports a duration; left as it is")
        new_dir = work / stable_id
        _part_bytes, settings, violations = _write_mp3_bundle(
            bundle, stable_id, old_raw, source_path, source_digest, source_bytes, duration_s, work
        )

        def source_unchanged() -> None:
            # The encode ran without the lock, so state.db may have relinked
            # the track, or the source file been rewritten, since the snapshot
            # (Codex on #6821). Re-read both under the swap's lock: the MP3
            # sizes were chosen against the source as it was. The content
            # hash catches a rewrite the size and mtime miss (Codex on #6820).
            library_now = library_paths(data_dir_of_bundle(bundle_dir), [stable_id]).get(stable_id)
            _require_still_eligible(snapshot_root, stable_id, candidate, library_now)
            if not source_path.is_file() or _source_signature(source_path) != source_signature:
                raise BundleChangedError(
                    f"{stable_id}: the source {source_path} changed while this pass encoded it; left as it is"
                )

        def source_bytes_unchanged() -> None:
            # Outside the lock: a whole mix is read, and the swap's re-check
            # of the source's marks under the lock catches a write after it.
            if sha256_file_hex(source_path) != source_digest:
                raise BundleChangedError(
                    f"{stable_id}: the source {source_path} changed while this pass encoded it; left as it is"
                )

        backup_dir = backup_root / root.name / stable_id
        before = _swap_in(
            new_dir,
            bundle_dir,
            backup_dir,
            read_fingerprint,
            before_swap,
            source_unchanged,
            source_bytes_unchanged=source_bytes_unchanged,
            source_path=source_path,
        )
        record_converted_bundle(data_dir_of_bundle(bundle_dir), bundle_dir)
        return Outcome(
            stable_id=stable_id,
            before_bytes=before,
            after_bytes=_dir_bytes(bundle_dir),
            source_bytes=source_bytes,
            mp3_settings=settings,
            violations=violations,
            backup_dir=backup_dir,
        )
    finally:
        _remove_work(work, stable_id, sys.exc_info()[1])


# ----- CLI ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the pass from the command line (:mod:`apps.stems.recompress_cli`)."""
    from apps.stems.recompress_cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())


def write_manifest(new_dir: Path, manifest: dict[str, Any]) -> None:
    """Write a re-encoded stem-set's manifest.json with pinned UTF-8 and LF bytes."""
    (new_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
