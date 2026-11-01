"""Energy backfill: give every present library row a MIK energy.

Four steps, each returning one JSON document that the CLI
(``python -m apps.mik backfill ...``), the engine routes (``/api/v1/energy``)
and the MCP tools (``energy_*``) all hand back unchanged:

``status``   where every present row stands (:mod:`apps.mik.backfill_picture`)
``plan``     the files that need a fresh analysis, one per recording
``stage``    copy the next N of them into a batch folder with ``.sources.txt``
``collect``  read a COPY of the MIK store back through the sources map

Rules this module does not bend:

* Mixed In Key rewrites every file it analyzes, so it only ever sees COPIES.
  The original files are opened for reading and nothing else.
* ``stage`` never launches Mixed In Key. It returns the exact command.
* ``stage`` and ``collect`` are dry runs unless ``live`` is set.
* ``collect`` deletes a copy only when asked, only inside the batch folder,
  and only when that copy's energy is in the library.
* Every refusal is an explicit :class:`BackfillError`. Nothing falls back.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from apps.shared.equivalence import EquivalenceGate
from apps.shared.state import db as state_db

from . import backfill_settings as settings
from . import duplicates as dups
from . import load as loader
from . import sources_match as sources
from .backfill_picture import (
    BackfillError,
    EnergyPicture,
    PlanEntry,
    energy_picture,
    plan_entries,
    read_store,
)
from .mikdb import MikSong
from .plan_rebind import plan_rebind


@dataclass(frozen=True)
class _Config:
    batch_prefix: str = sources.BATCH_PREFIX
    stage_lock_file: str = ".mik-stage.lock"
    batch_stamp_format: str = "%Y%m%d-%H%M"
    """UTC, to the minute: one batch folder per ``stage --live``."""
    manifest_file: str = sources.MANIFEST_FILE
    collected_file: str = ".collected.txt"
    default_limit: int = 100
    max_limit: int = 500
    mik_app: str = "Mixed In Key 11"
    collect_fields: frozenset[str] = frozenset({"energy", loader.SEGMENTS_FIELD})
    mount_parents: tuple[Path, ...] = (Path("/Volumes"), Path("/media"), Path("/mnt"))
    """Folders whose children are mounted drives; a missing child is a disconnected drive."""
    plan_preview: int = 20
    """Entries listed by ``plan`` when no ``--limit`` is given."""


CFG = _Config()


@dataclass(frozen=True)
class Library:
    """The library a backfill step reads or writes."""

    state_db: Path
    data_dir: Path

    @classmethod
    def at(cls, data_dir: Path) -> Library:
        return cls(state_db=data_dir / "state" / "state.db", data_dir=data_dir)


@contextmanager
def _reading(library: Library) -> Iterator[sqlite3.Connection]:
    conn = state_db.open_ro(library.state_db)
    try:
        yield conn
    finally:
        conn.close()


def _checked_limit(limit: int | None, default: int) -> int:
    value = default if limit is None else limit
    if not 1 <= value <= CFG.max_limit:
        raise BackfillError(f"limit must be between 1 and {CFG.max_limit}, got {value}")
    return value


# ------------------------------------------------------------ open batches


def open_batch_sources(ingest_root: Path, *, platform: str) -> dict[str, str]:
    """``{source path KEY: batch folder}`` for every batch folder still on disk.

    A file already copied into a batch is not planned again until that batch
    is collected, or the same files would be staged by every call.
    """
    listed: dict[str, str] = {}
    if not ingest_root.is_dir():
        return listed
    for folder in sorted(ingest_root.glob(f"{CFG.batch_prefix}*")):
        if (folder / sources.SOURCES_FILE).is_file():
            closed = _collected_lines(folder)
            for line in sources.read_source_lines(folder):
                if line not in closed:
                    listed.setdefault(sources.path_key(line, platform=platform) or line, str(folder))
    return listed


def _collected_lines(folder: Path) -> set[str]:
    """Source lines a live collect already delivered from ``folder``: no longer open."""
    path = folder / CFG.collected_file
    if not path.is_file():
        return set()
    return {line for line in path.read_text(encoding="utf-8").split("\n") if line}


def _record_collected(batch_dir: Path, lines: set[str]) -> None:
    """Close delivered sources so a re-ingested original can be staged again later.

    Appended in one write, never rewritten: two collects settling the same
    batch at once each add their own lines, so neither can erase the other's.
    A line closed twice is harmless, as readers take the set.
    """
    if not lines:
        return
    with (batch_dir / CFG.collected_file).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(f"{line}\n" for line in sorted(lines)))


# ----------------------------------------------------------- status + plan


def status(library: Library, *, share_across_duplicates: bool = False) -> dict[str, Any]:
    """Coverage from the library alone, against the ``present`` denominator."""
    with _reading(library) as conn:
        picture = energy_picture(conn, by_metadata=share_across_duplicates)
    return {"state_db": str(library.state_db), **picture.counts(), "equivalence": gate_preview(library)}


def gate_preview(library: Library) -> dict[str, str]:
    """The equivalence verdict for the fields ``collect`` writes, before any work.

    A preview only: ``collect`` also checks the verdict was proven against
    the store file it is given, which cannot be known until then. Anything
    other than ``passed`` here means ``collect`` will write nothing.
    """
    return EquivalenceGate.load(library.data_dir).summary(sorted(CFG.collect_fields))


def _picture_and_entries(
    library: Library, *, store: Path | None, share_across_duplicates: bool, ingest_root: Path
) -> tuple[EnergyPicture, list[PlanEntry], dict[str, int]]:
    with _reading(library) as conn:
        picture = energy_picture(conn, store=store, by_metadata=share_across_duplicates)
    in_batches = open_batch_sources(ingest_root, platform=sources.HOST_PLATFORM)
    entries: list[PlanEntry] = []
    held = {"already_in_an_open_batch": 0, "no_checked_path": 0}
    for entry in plan_entries(picture):
        if entry.source is None:
            held["no_checked_path"] += 1
        elif sources.path_key(entry.source, platform=sources.HOST_PLATFORM) in in_batches:
            held["already_in_an_open_batch"] += 1
        else:
            entries.append(entry)
    return picture, entries, held


def plan(
    library: Library,
    *,
    limit: int | None = None,
    store: Path | None = None,
    share_across_duplicates: bool = False,
    ingest_root: Path | None = None,
) -> dict[str, Any]:
    """What a fresh analysis has to cover. Read-only.

    ``store`` is a MIK store NOT yet loaded: rows it would settle are counted
    under ``without_mik_energy`` and left out of the plan.
    """
    shown = _checked_limit(limit, CFG.plan_preview)
    root = settings.resolve(library.data_dir, ingest_root=ingest_root).ingest_root
    picture, entries, held = _picture_and_entries(
        library, store=store, share_across_duplicates=share_across_duplicates, ingest_root=root
    )
    return {
        "state_db": str(library.state_db),
        **picture.counts(),
        "equivalence": gate_preview(library),
        "to_analyze": {
            "files": len(entries),
            "rows": sum(len(entry.stable_ids) for entry in entries),
            "held_back": held,
        },
        "limit": shown,
        "next": [entry.payload() for entry in entries[:shown]],
    }


# ------------------------------------------------------------------- stage


def _roots_of(source: str, stored_paths: tuple[str, ...], platform: str) -> set[str]:
    """Every library root (as a path KEY) under which ``collect`` could bind ``source``.

    ``""`` means a row is stored under the source path itself (no root
    needed). Otherwise a root is what is left of the source once a row's path
    below its ``/Users/<name>`` home is taken off the end. Empty when no row
    of the entry can be reached that way: ``collect`` could never bind it.
    """
    line = sources.path_key(source, platform=platform)
    roots: set[str] = set()
    for stored in stored_paths:
        path = sources.path_key(stored, platform=platform)
        if line is None or path is None:
            continue
        if line == path:
            roots.add("")
            continue
        below_home, replaced = sources.strip_home(path, platform=platform)
        if replaced and line.endswith(below_home) and len(line) > len(below_home):
            roots.add(line[: -len(below_home)])
    return roots


@dataclass
class _Selection:
    chosen: list[tuple[PlanEntry, Path]]
    library_root: str
    skipped: dict[str, int]
    total_bytes: int


_LINE_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
"""Every character ``str.splitlines`` breaks on: the sources file holds one path per line."""


def _bindable(entries: list[PlanEntry], skipped: dict[str, int]) -> list[tuple[PlanEntry, Path, set[str]]]:
    """Entries that are on disk now and that ``collect`` could bind, with their roots."""
    rooted: list[tuple[PlanEntry, Path, set[str]]] = []
    for entry in entries:
        path = Path(entry.source or "")
        roots = _roots_of(str(path), entry.stored_paths, sources.HOST_PLATFORM)
        if _LINE_BREAKS.intersection(str(path)):
            skipped["line_break_in_path"] += 1  # one source per line: such a name cannot round-trip
        elif not path.is_file():
            skipped["source_missing"] += 1
        elif not roots:
            skipped["unmappable_source"] += 1
        else:
            rooted.append((entry, path, roots))
    return rooted


def _most_voted_root(rooted: list[tuple[PlanEntry, Path, set[str]]]) -> str:
    """The library root most entries can be bound under; ``""`` when none needs one."""
    votes: dict[str, int] = {}
    for _entry, _path, roots in rooted:
        for root in roots - {""}:
            votes[root] = votes.get(root, 0) + 1
    return max(sorted(votes), key=lambda root: votes[root]) if votes else ""


def _batch_root_as_written(rooted: list[tuple[PlanEntry, Path, set[str]]], batch_root: str) -> str:
    """The batch's root spelled as its first source under it writes it; ``""`` when it needs none."""
    for _entry, path, roots in rooted:
        if batch_root and batch_root in roots:
            return _root_as_written(str(path), batch_root, sources.HOST_PLATFORM)
    return ""


def _root_as_written(source: str, root_key: str, platform: str) -> str:
    """``root_key`` spelled as ``source`` writes it: the manifest and reports show this, never a key."""
    written = PurePosixPath(sources.path_spelling(source) or "")
    below = len(PurePosixPath(sources.path_key(source, platform=platform) or "").parts) - len(
        PurePosixPath(root_key).parts
    )
    return str(PurePosixPath(*written.parts[: len(written.parts) - below]))


def _select(entries: list[PlanEntry], limit: int) -> _Selection:
    """The next ``limit`` files one batch can hold, and why the rest wait.

    One batch has ONE library root and flat copies, so a file under another
    root, or sharing a basename with one already chosen, waits for a later
    batch. A file that is not on disk now is skipped and counted.
    """
    skipped = {
        "line_break_in_path": 0,
        "source_missing": 0,
        "unmappable_source": 0,
        "other_library_root": 0,
        "same_basename": 0,
    }
    rooted = _bindable(entries, skipped)
    batch_root = _most_voted_root(rooted)
    chosen: list[tuple[PlanEntry, Path]] = []
    names: set[str] = set()
    for entry, path, roots in rooted:
        if len(chosen) >= limit:
            break
        if "" not in roots and batch_root not in roots:
            skipped["other_library_root"] += 1
        elif (name := sources.path_key(path.name, platform=sources.HOST_PLATFORM) or "") in names:
            skipped["same_basename"] += 1
        else:
            names.add(name)
            chosen.append((entry, path))
    total = sum(path.stat().st_size for _entry, path in chosen)
    return _Selection(chosen, _batch_root_as_written(rooted, batch_root), skipped, total)


def _unmounted_volume(folder: Path, mount_parents: tuple[Path, ...]) -> Path | None:
    """The unmounted volume ``folder`` lives on, or None.

    A path under a mount parent (``/Volumes`` on macOS) whose volume folder is
    not a mount point belongs to a disconnected drive, even when a stale plain
    folder of that name is left behind. Writing there would put the batch on
    the system disk, where it would be hidden once the drive came back.
    ``folder`` is resolved first, so a symlink such as ``~/ingest ->
    /Volumes/Scratch`` is judged by where it really points.
    """
    folder = folder.resolve()
    for parent in (mount.resolve() for mount in mount_parents):
        try:
            relative = folder.relative_to(parent)
        except ValueError:
            continue
        volume = parent / relative.parts[0] if relative.parts else None
        if volume is not None and not os.path.ismount(volume):
            return volume
    return None


def _free_bytes(folder: Path) -> int:
    volume = _unmounted_volume(folder, CFG.mount_parents)
    if volume is not None:
        raise BackfillError(
            f"the ingest root {folder} is on {volume}, which is not mounted: connect the drive "
            "or choose another ingest root"
        )
    probe = folder
    while not probe.exists():
        if probe.parent == probe:
            raise BackfillError(f"no existing directory above {folder} to measure free disk on")
        probe = probe.parent
    return shutil.disk_usage(probe).free


_Copies = list[tuple[PlanEntry, Path, Path]]


def _write_batch(batch_dir: Path, copies: _Copies, library_root: str, stamp: str) -> None:
    """Make the batch folder: the copies, ``.sources.txt`` and the manifest."""
    try:
        batch_dir.mkdir(parents=True)
    except FileExistsError as error:
        raise BackfillError(f"batch folder {batch_dir} already exists; wait a minute or collect it first") from error
    try:
        for _entry, path, copy in copies:
            shutil.copy2(path, copy)
        (batch_dir / sources.SOURCES_FILE).write_text(
            "".join(f"{path}\n" for _entry, path, _copy in copies), encoding="utf-8", newline="\n"
        )
        (batch_dir / CFG.manifest_file).write_text(
            json.dumps({"library_root": library_root or None, "files": len(copies), "created_utc": stamp}),
            encoding="utf-8",
            newline="\n",
        )
    except BaseException:
        # A folder with no manifest is invisible to open-batch accounting, so
        # its copies would be orphaned; remove it and surface the error.
        shutil.rmtree(batch_dir)
        raise


MIK_LAUNCH_PLATFORM = "darwin"
"""The copy-batch loop is macOS-only: the reader understands Mixed In Key for
Mac's Core Data store (``Collection*.mikdb``). Mixed In Key for Windows keeps a
different database (``MIKStore.db``) that nothing here reads, so a Windows
launch command would start a loop that could never be collected."""


def _open_command(copies: _Copies, *, platform: str) -> dict[str, Any]:
    """The exact macOS command that queues the copies in Mixed In Key. Never run from here.

    The command is always the macOS one and says so (``launch_platform``). On
    any other host ``host_supported`` is false and ``next_step`` says where it
    has to run, rather than handing out a command that cannot work here.
    """
    if not copies:
        return {"open_argv": None, "open_command": None, "next_step": "nothing to stage"}
    argv = ["open", "-a", CFG.mik_app, *(str(copy) for _entry, _path, copy in copies)]
    supported = platform == MIK_LAUNCH_PLATFORM
    next_step = (
        "run open_command yourself (or ask the user to), wait for Mixed In Key to finish, "
        "then collect this batch_dir with a COPY of the MIK store"
        if supported
        else (
            f"this host is {platform!r}; the copy-batch loop needs Mixed In Key for Mac, whose "
            "Collection*.mikdb is the only store this reader understands. Run open_command on a "
            "Mac that can read these copies, then collect with a COPY of that Mac's store"
        )
    )
    return {
        "open_argv": argv,
        "open_command": shlex.join(argv),
        "launch_platform": MIK_LAUNCH_PLATFORM,
        "host_supported": supported,
        "next_step": next_step,
    }


# Windows reports a lock held elsewhere as one of these once LK_LOCK's own ten tries run out.
_LOCK_BUSY_ERRNOS = frozenset({errno.EDEADLK, errno.EACCES})


def _lock_windows(msvcrt: Any, descriptor: int, *, pause_s: float = 1.0) -> None:
    """Take the one-byte stage lock, waiting however long a peer stage holds it.

    Only contention is retried: a seek or filesystem failure raises at once
    rather than leaving the stage spinning.
    """
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        try:
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        except OSError as error:
            if error.errno not in _LOCK_BUSY_ERRNOS:
                raise
            time.sleep(pause_s)
        else:
            return


@contextmanager
def _stage_lock(data_dir: Path) -> Iterator[None]:
    """One live stage at a time per library, from planning through batch publication.

    Two concurrent stages would plan the same files before either writes its
    sources file and copy them into two batches, or race for one minute's
    batch folder. Cross-process, as the engine route and the CLI both stage.
    """
    path = data_dir / CFG.stage_lock_file
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            _lock_windows(msvcrt, descriptor)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)  # closing the descriptor releases either lock


def stage(
    library: Library,
    *,
    limit: int | None = None,
    live: bool = False,
    store: Path | None = None,
    share_across_duplicates: bool = False,
    ingest_root: Path | None = None,
    min_free_bytes: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Copy the next planned files into a new batch folder. Dry run unless ``live``.

    ``ingest_root`` and ``min_free_bytes`` override the library's stored
    settings (:mod:`apps.mik.backfill_settings`) for this call. A live stage
    holds :func:`_stage_lock` for its whole run.
    """
    if not live:
        return _stage_unlocked(library, limit, live, store, share_across_duplicates, ingest_root, min_free_bytes, now)
    with _stage_lock(library.data_dir):
        return _stage_unlocked(library, limit, live, store, share_across_duplicates, ingest_root, min_free_bytes, now)


def _stage_unlocked(
    library: Library,
    limit: int | None,
    live: bool,
    store: Path | None,
    share_across_duplicates: bool,
    ingest_root: Path | None,
    min_free_bytes: int | None,
    now: datetime | None,
) -> dict[str, Any]:
    count = _checked_limit(limit, CFG.default_limit)
    chosen = settings.resolve(library.data_dir, ingest_root=ingest_root, min_free_bytes=min_free_bytes)
    root = chosen.ingest_root
    _picture, entries, held = _picture_and_entries(
        library, store=store, share_across_duplicates=share_across_duplicates, ingest_root=root
    )
    selection = _select(entries, count)
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime(CFG.batch_stamp_format)
    batch_dir = root / f"{CFG.batch_prefix}{stamp}"
    if batch_dir.exists():
        raise BackfillError(f"batch folder {batch_dir} already exists; wait a minute or collect it first")
    free = _free_bytes(root)
    settings.check_disk_floor(
        free_bytes=free, needed_bytes=selection.total_bytes, min_free_bytes=chosen.min_free_bytes, where=root
    )
    copies: _Copies = [(entry, path, batch_dir / path.name) for entry, path in selection.chosen]
    copied = bool(live and copies)
    if copied:
        _write_batch(batch_dir, copies, selection.library_root, stamp)
    return {
        "mode": "live" if live else "dry-run",
        "state_db": str(library.state_db),
        "batch_dir": str(batch_dir),
        "library_root": selection.library_root or None,
        "files": [
            {"source": str(path), "copy": str(copy), "stable_ids": list(entry.stable_ids)}
            for entry, path, copy in copies
        ],
        "counts": {
            "staged": len(copies),
            "rows_settled": sum(len(entry.stable_ids) for entry, _p, _c in copies),
            "planned_files": len(entries),
            "held_back": {**held, **selection.skipped},
        },
        "bytes": selection.total_bytes,
        "free_bytes_before": free,
        "min_free_bytes": chosen.min_free_bytes,
        "copied": copied,
        **_open_command(copies, platform=sources.HOST_PLATFORM),
    }


# ----------------------------------------------------------------- collect


@contextmanager
def _store_to_read(store: Path, *, copy_first: bool) -> Iterator[Path]:
    """``store`` itself, or a consistent snapshot of it.

    Mixed In Key's live store is never opened from here for writing. With
    ``copy_first`` the store is snapshotted through SQLite's own backup
    mechanism into a single file, so a write or checkpoint in the middle can
    never pair an old main file with a newer ``-wal``.

    A store whose ``-wal`` still holds committed changes is refused either
    way: its main file, which is all a verdict fingerprint describes, is not
    the database that would be read. Mixed In Key merges its ``-wal`` when it
    quits, which is also when its newest rows reliably appear.
    """
    if not store.is_file():
        raise BackfillError(f"MIK store {store} does not exist")
    before = _quiescent_store_state(store)
    if not copy_first:
        yield store
        # A writer may have created or appended the -wal while it was read;
        # the caller must not apply anything until this passes.
        assert_store_unchanged(store, before)
        return
    with tempfile.TemporaryDirectory(prefix="opendj-mik-store-") as work:
        snapshot = Path(work) / store.name
        source = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")  # one standalone file, no -wal when read
        finally:
            target.close()
            source.close()
        assert_store_unchanged(store, before)
        yield snapshot
        # The caller fingerprints ``store`` for the gate while this block is
        # open; the original must still be the bytes the snapshot copied.
        assert_store_unchanged(store, before)


def _quiescent_store_state(store: Path) -> str:
    """A sha256 of the main file, refusing a store whose ``-wal`` holds changes.

    A content digest, not (size, mtime): Linux advances mtime once per kernel
    tick, so a commit landing in the same tick as the first reading would
    leave both unchanged and pass unseen.
    """
    wal = store.with_name(store.name + "-wal")
    if wal.is_file() and wal.stat().st_size > 0:
        raise BackfillError(
            f"{wal} holds {wal.stat().st_size} bytes of changes not yet merged into {store.name}: "
            "quit Mixed In Key (it merges them on quit), then copy the store again with cp -p"
        )
    with store.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def assert_store_unchanged(store: Path, before: str) -> None:
    """Refuse a snapshot taken while the store was written to.

    The verdict fingerprint describes the main file as it was before the
    backup. A commit landing during the backup would put rows in the snapshot
    that fingerprint never saw, so the main file's digest and an empty
    ``-wal`` are measured again afterwards.
    """
    if _quiescent_store_state(store) != before:
        raise BackfillError(
            f"{store} changed while it was being snapshotted: quit Mixed In Key and run collect again"
        )


def _library_root(batch_dir: Path, given: Path | None) -> Path | None:
    """The root to strip from source lines; ``None`` when the batch needs none."""
    if given is not None:
        return given
    manifest = batch_dir / CFG.manifest_file
    if not manifest.is_file():
        raise BackfillError(
            f"{batch_dir} has no {CFG.manifest_file} (it was not made by stage); pass the library root"
        )
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise BackfillError(f"{manifest} is not valid JSON ({error})") from error
    if not isinstance(payload, dict) or "library_root" not in payload:
        raise BackfillError(f"{manifest} has no library_root key")
    recorded = payload["library_root"]
    if recorded is not None and not isinstance(recorded, str):
        raise BackfillError(f"{manifest}: library_root must be a path string or null, not {recorded!r}")
    return Path(recorded) if recorded else None


def _delivered_copies(
    batch: sources.SourcesMatch,
    share: dups.ShareResult,
    plan: loader.LoadPlan,
    batch_dir: Path,
    held: frozenset[str] = frozenset(),
) -> list[Path]:
    """Copies whose energy scalar AND series are headed into the library, inside the batch folder only.

    A copy counts when the row it resolved to, or a row it shares with (one
    file behind several rows), has its energy in the plan, and every such row
    also has its series in the plan. A scalar alone puts the track in
    ``with_mik_energy``, so deleting the copy then would strand the series:
    no later plan would stage that file again.
    """
    scalar = {write.stable_id for write in plan.field_writes if write.field_name == "energy"} - held
    series = {write.stable_id for write in plan.segment_writes} - held
    folder = batch_dir.resolve()
    return [
        copy
        for song in batch.songs
        if _fully_delivered(_rows_reached(batch, share, song), scalar, series, held)
        and (copy := _own_copy(song, folder)) is not None
    ]


def _delivered_sources(
    batch: sources.SourcesMatch, share: dups.ShareResult, plan: loader.LoadPlan, held: frozenset[str] = frozenset()
) -> set[str]:
    """Source lines whose analysis (scalar AND series) is headed into the library.

    ``held`` rows were dropped at write time (see ``ApplyResult.held_rows``), so
    their sources stay open for the next run to re-plan.
    """
    scalar = {write.stable_id for write in plan.field_writes if write.field_name == "energy"} - held
    series = {write.stable_id for write in plan.segment_writes} - held
    return {
        batch.sources[song.row_id]
        for song in batch.songs
        if song.row_id in batch.sources and _fully_delivered(_rows_reached(batch, share, song), scalar, series, held)
    }


def _rows_reached(batch: sources.SourcesMatch, share: dups.ShareResult, song: MikSong) -> set[str]:
    """The rows a song's analysis lands on: its own match plus the rows it shares to."""
    reached = {target.stable_id for target in share.shared.get(song.row_id, ())}
    match = batch.report.matches.get(song.row_id)
    if match is not None:
        reached.add(match.stable_id)
    return reached


def _fully_delivered(reached: set[str], scalar: set[str], series: set[str], held: frozenset[str]) -> bool:
    """Every reached row that still needs this analysis got scalar AND series.

    A held row (dropped at write time) still needs it, so its copy and source
    stay for the next run even when another reached row was written.
    """
    delivered = reached & scalar
    return bool(delivered) and delivered <= series and not (reached & held)


def _own_copy(song: MikSong, folder: Path) -> Path | None:
    """The song's file when it is a real file directly inside the batch folder."""
    if not song.path:
        return None
    copy = Path(song.path)
    if copy.is_file() and not copy.is_symlink() and copy.resolve().parent == folder:
        return copy
    return None


@dataclass(frozen=True)
class CollectRequest:
    """What ``collect`` is asked to do. Everything beyond the two paths is off by default."""

    batch_dir: Path
    store: Path
    """A COPY of the MIK store, unless ``copy_store_first``."""
    library_root: Path | None = None
    live: bool = False
    delete_copies: bool = False
    share_across_duplicates: bool = False
    allow_unverified: bool = False
    copy_store_first: bool = False


def _applied_document(applied: loader.ApplyResult | None) -> dict[str, int | bool] | None:
    if applied is None:
        return None
    return {
        "fields_written": applied.fields_written,
        "fields_unchanged": applied.fields_unchanged,
        "segment_tracks": applied.segment_tracks,
        "segment_rows": applied.segment_rows,
        "segment_tracks_unchanged": applied.segment_tracks_unchanged,
        "staged_written": applied.staged_written,
        "stale_plan": applied.stale_plan,
    }


def _plan_document(load_plan: loader.LoadPlan) -> dict[str, Any]:
    return {
        "equivalence": load_plan.gate_summary,
        "planned": {
            "field_writes": len(load_plan.field_writes),
            "segment_tracks": len(load_plan.segment_writes),
            "segment_rows": load_plan.segment_row_count,
            "staged_rows": len(load_plan.staged),
        },
        "blocked_by_gate": load_plan.blocked_by_gate,
        "blocked_by_precedence": load_plan.blocked_by_precedence,
        "source_had_no_value": load_plan.no_value,
    }


def _delete_copies(copies: list[Path]) -> int:
    for copy in copies:
        # A peer collect of the same batch may have delivered and removed it first.
        copy.unlink(missing_ok=True)
    return len(copies)


def _settle_delivery(
    batch: sources.SourcesMatch,
    share: dups.ShareResult,
    plan: loader.LoadPlan,
    batch_dir: Path,
    applied: loader.ApplyResult | None,
    request: CollectRequest,
) -> tuple[list[Path], int]:
    """Close the delivered sources and delete their copies when live: (deliverable, deleted)."""
    if applied is not None and applied.stale_plan:
        return [], 0  # a refused plan wrote nothing, so nothing in it was delivered
    held = applied.held_rows if applied is not None else frozenset()
    deliverable = _delivered_copies(batch, share, plan, batch_dir, held)
    if applied is None:
        return deliverable, 0
    _record_collected(batch_dir, _delivered_sources(batch, share, plan, held))
    return deliverable, _delete_copies(deliverable) if request.delete_copies else 0


def collect(library: Library, request: CollectRequest) -> dict[str, Any]:
    """Load a batch's energy through its sources map. Dry run unless ``live``."""
    batch_dir, store, live = request.batch_dir, request.store, request.live
    if not batch_dir.is_dir():
        raise BackfillError(f"batch folder {batch_dir} does not exist")
    root = _library_root(batch_dir, request.library_root)
    with _store_to_read(store, copy_first=request.copy_store_first) as readable:
        songs = read_store(readable)
        # The verdict fingerprints the store the caller named; a snapshot taken
        # by copy_store_first is a new file and could never match it. Built
        # inside the block, so the store is re-checked unchanged after it: the
        # fingerprint the gate trusts belongs to the bytes that were read.
        gate = EquivalenceGate.load(
            library.data_dir, allow_unverified=request.allow_unverified, sources={"mik": store}
        )
    conn = state_db.open_rw(library.state_db) if live else state_db.open_ro(library.state_db)
    try:
        batch = sources.match_songs_by_sources(
            songs, conn, batch_dir=batch_dir, library_root=root, platform=sources.HOST_PLATFORM
        )
        share = dups.plan_sharing(
            batch.songs,
            batch.report,
            dups.duplicate_groups(
                conn, by_metadata=request.share_across_duplicates),
            candidates_of=batch.candidates_of,
        )
        load_plan = loader.build_plan(
            batch.songs, batch.report, gate, conn, fields=CFG.collect_fields, shared=share.shared
        )
        load_plan.still_valid = dups.binding_unchanged(
            plan_rebind(songs, sources_batch=batch_dir, library_root=root),
            batch.report,
            share.shared,
            by_metadata=request.share_across_duplicates,
        )
        applied = loader.apply_plan(conn, load_plan, actor="apps.mik.backfill") if live else None
    finally:
        conn.close()
    deliverable, deleted = _settle_delivery(batch, share, load_plan, batch_dir, applied, request)
    unmatched = batch.report.unmatched
    return {
        "mode": "live" if live else "dry-run",
        "state_db": str(library.state_db),
        "batch_dir": str(batch_dir),
        "store": str(store),
        "library_root": str(root) if root is not None else None,
        "sources_batch": batch.payload(),
        "unresolved": [
            {"mik_row_id": song.row_id, "copy": song.path, "detail": unmatched[song.row_id].detail}
            for song in batch.songs
            if song.row_id in unmatched and song.row_id not in share.shared
        ],
        "duplicate_sharing": share.counts(),
        **_plan_document(load_plan),
        "applied": _applied_document(applied),
        "copies": {
            "deletable": len(deliverable),
            "deleted": deleted,
            "delete_requested": request.delete_copies,
        },
    }


__all__ = [
    "CFG",
    "BackfillError",
    "CollectRequest",
    "Library",
    "collect",
    "open_batch_sources",
    "plan",
    "stage",
    "status",
]
