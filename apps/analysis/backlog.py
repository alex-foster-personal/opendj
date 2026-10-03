"""The rekordbox-unmapped analysis backlog: what still has to be analyzed.

A track imported straight into the local library (folder ingest, a generated
e2e fixture library, stems written into ``state.db``) gets a ``tracks`` row
and no live ``rekordbox`` ``track_vendor_ids`` row, so it has no rekordbox twin
and no ANLZ to read a beatgrid, waveform or cue out of. "Unmapped" throughout
this module means unmapped TO REKORDBOX (:data:`ANLZ_VENDOR`) specifically: a
djay- or serato-only mapping supplies no ANLZ, so it is work, not an excuse.

:mod:`apps.analysis` is the answer to that, but only once a row exists for the
track to serve, and nothing ran it on import. This module is the QUEUE half of
that fix: the derived set of tracks that landed without a rekordbox mapping and
still have no analysis row.

Derived, never stored. There is no queue table to drift out of sync with the
library: the backlog is a projection of ``tracks`` + ``track_vendor_ids`` +
``analysis`` computed on demand, so an import through ANY path (CLI, setup
wizard, upload, fixture generator) is visible to the next scan with no
enqueue step to forget.

Denominators are named, per the repo's honest-figures rule. ``unmapped`` is
the whole vendor-unmapped, locally-playable population; each of its members
falls in exactly one bucket, tested in this order:

  1. ``analyzed``    - already has an ``analysis`` row. Nothing to do.
  2. ``unreachable`` - no materialized local bytes (broken link, iCloud
     placeholder). Cannot be analyzed; queueing it would retry forever.
  3. ``pending_total`` - the work queue. ``analyzed + unreachable +
     pending_total`` always equals ``unmapped``.

Streaming rows (``spotify:`` / ``tidal:`` / ``soundcloud:`` URIs) are outside
the population entirely: there are no local bytes to decode, so they are not
work and not a broken link either.

Requirements (mini-PRD):
  ✔︎ ✅ scan(): the backlog, its buckets and a change signature.
    [if] a track carries a live rekordbox track_vendor_ids row [then] it is
      absent from every bucket
    [if] a track's only mapping is to another vendor (djay, serato, traktor)
      [then] it is still pending, because no ANLZ exists for it
    [if] a vendor row is soft-deleted [then] the track is unmapped again
    [if] the ``analysis`` table has never been created [then] every unmapped
      track reads as pending, never an error
    [if] a file is not materialized [then] it counts unreachable, not pending
    [if] ``limit`` is passed [then] only the listed items shrink, never the
      denominators and never the change signature
    [if] a pending track is relinked to a different file [then] the signature
      changes even though its stable_id did not
    [if] a pending track's file is repaired IN PLACE [then] the signature
      changes even though neither its stable_id nor its path did
    [if] the platform's ctime cannot see an in-place repair [then] the token
      digests the bytes instead, so the repair is still visible
    [if] the only analysis row is from another backend [then] the track is
      still pending, because the drain would analyze it
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from apps.shared import fs_residency, platform_paths
from apps.shared.state import locations as track_locations

from .backends import DEFAULT_BACKEND

#: The backend the drain actually runs. ``_run_analysis_chunk`` shells out to
#: ``apps.analysis.run`` with no ``--backend``, and that CLI's own
#: ``filter_missing`` drops a track only when a row exists for the backend it
#: is about to run. This queue asks the SAME question, so the two can never
#: disagree about what "already analyzed" means: a ``mik`` row is not a
#: librosa row, and MikBackend copies whatever the CLI emitted - an empty
#: ``downbeats_s`` makes ``/beatgrid-fallback`` 404 with
#: BEATGRID_FALLBACK_NOT_FOUND, so treating it as done would strand the track
#: without a grid forever.
DRAIN_BACKEND: str = DEFAULT_BACKEND

#: The resolver reason meaning "this row names a service URI, not a file".
#: ``resolve_library_path`` answers it for the streaming schemes AND for an
#: empty path, which the SQL cannot reject on its own: it only filters NULL,
#: and ``Path("")`` is ``Path(".")``, so a pathless track would otherwise be
#: probed as though a directory were audio.
STREAMING_REASON: str = "streaming"

#: The one vendor whose mapping means "an ANLZ already exists for this track".
#: ``track_vendor_ids.vendor`` is free text and the agentbox crate replication
#: writes whatever vendor its payload names, so a djay-, serato- or
#: traktor-only row is NOT evidence of a beatgrid. The browser draws the same
#: line: ``rb_vendor_pkg/track_rows.py`` resolves ``has_rb_mapping`` from a
#: ``vendor = 'rekordbox'`` lookup and names a djay-only mapping as a real
#: library state with nothing to serve.
ANLZ_VENDOR: str = "rekordbox"

_UNMAPPED_SQL = """
SELECT t.stable_id, t.file_path, t.title
FROM tracks AS t
LEFT JOIN track_vendor_ids AS v
       ON v.stable_id = t.stable_id
      AND v.vendor = :anlz_vendor
      AND v.deleted_at IS NULL
WHERE t.deleted_at IS NULL
  AND v.stable_id IS NULL
ORDER BY t.stable_id
"""


@dataclass(frozen=True)
class BacklogItem:
    """One queued track. ``file_path`` is what the runner decodes.

    ``content_token`` is ``"<size>:<mtime_ns>:<ctime_ns>"`` of that file. It
    exists for the change signature: a track repaired IN PLACE keeps both its
    stable_id and its path, so identity alone cannot tell the auto-drain that
    the bytes it failed on are no longer the bytes on disk. See
    :func:`_content_token` for why ctime is in there and what it does not
    cover.
    """

    stable_id: str
    file_path: str
    title: str | None
    content_token: str


@dataclass(frozen=True)
class Backlog:
    """One scan. ``analyzed + unreachable + pending_total`` == ``unmapped``.

    ``pending`` is the LISTED slice (``limit`` applies to it alone) and
    ``pending_total`` is the real size of the queue, so a paged read can
    never be mistaken for a short backlog.
    """

    pending: tuple[BacklogItem, ...]
    pending_total: int
    unmapped: int
    analyzed: int
    unreachable: int
    signature: str


def analyzed_ids(conn: sqlite3.Connection, *, backend: str = DRAIN_BACKEND) -> set[str]:
    """stable_ids with an analysis row FOR THE BACKEND THE DRAIN RUNS.

    Scoped to one backend on purpose; see :data:`DRAIN_BACKEND`. Asking "any
    analysis row at all" would let a mik row retire a track the librosa drain
    would happily have analyzed, and mik rows can carry no usable downbeats.

    :mod:`apps.analysis.store` creates the ``analysis`` table on first write,
    so its absence means zero tracks analyzed, not a broken database.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis'"
    ).fetchone() is not None
    if not has_table:
        return set()
    return {
        row[0] for row in conn.execute(
            "SELECT DISTINCT stable_id FROM analysis WHERE backend = ?", (backend,)
        )
    }


def scan(conn: sqlite3.Connection, *, limit: int | None = None) -> Backlog:
    """Compute the backlog. ``limit`` caps ``pending`` only, never the counts.

    Every row goes through ``platform_paths.resolve_library_path`` before it
    is probed, because the stored ``file_path`` is not always a path on THIS
    machine: agentbox and a Windows install read a library whose paths are a
    Mac's, and ``MDT_PATH_MAP`` is what turns one into the other. Probing the
    raw string would count every track on those deployments as unreachable
    and never analyze any of them. The queue item carries the RESOLVED path,
    because that string is what ``--pairs-json`` hands the analyzer to decode.

    The path map is loaded once per scan rather than once per row: this walks
    a whole library, and the resolver would otherwise re-read the JSON file
    for every track.
    """
    if limit is not None and limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")
    done = analyzed_ids(conn)
    path_map = platform_paths.load_path_map()
    pending: list[BacklogItem] = []
    unmapped = 0
    analyzed = 0
    unreachable = 0
    rows = conn.execute(_UNMAPPED_SQL, {"anlz_vendor": ANLZ_VENDOR}).fetchall()
    locations = _local_location_paths(conn, [sid for sid, _, _ in rows])
    for stable_id, file_path, title in rows:
        mapped = [
            platform_paths.resolve_library_path(candidate, path_map=path_map)
            for candidate in _candidate_paths(
                file_path, locations.get(stable_id, ())
            )
        ]
        local = [m for m in mapped if m.reason != STREAMING_REASON]
        if not local:
            # Streaming-only, or no stored path at all. Neither is a local
            # file the analyzer could ever decode, so neither is a backlog.
            continue
        unmapped += 1
        if stable_id in done:
            analyzed += 1
            continue
        item = _first_playable(stable_id, local, title)
        if item is None:
            unreachable += 1
        else:
            pending.append(item)
    return Backlog(
        pending=tuple(pending if limit is None else pending[:limit]),
        pending_total=len(pending),
        unmapped=unmapped,
        analyzed=analyzed,
        unreachable=unreachable,
        signature=_signature(pending),
    )


def _local_location_paths(
    conn: sqlite3.Connection, stable_ids: list[str]
) -> dict[str, list[str]]:
    """Extra playable copies on THIS machine, keyed by stable_id.

    Reuses the production helper rather than a second query of its table, so
    the drain and the browser row agree on what "present here" means -
    ``rb_vendor_pkg/track_rows.py`` ORs these same paths with the legacy
    column when it decides a track is playable. It filters by machine_id, so
    a copy that lives on another machine never reads as local.
    """
    if not stable_ids:
        return {}
    return track_locations.list_location_paths(conn, stable_ids)


def _candidate_paths(
    legacy: str | None, location_paths: Sequence[str]
) -> list[str]:
    """Every path this track might be decodable from, best guess first.

    ``tracks.file_path`` leads because it is the ingest path and is right for
    most rows, but it is not authoritative: ``upsert_track_location`` writes a
    real local copy WITHOUT touching that column, so after a hydrate or a sync
    the legacy value can be null or stale while the file is really here.
    Empties are dropped rather than probed, which is what keeps an empty
    column from being resolved against the current directory.
    """
    seen: set[str] = set()
    out: list[str] = []
    for path in (legacy, *location_paths):
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _first_playable(
    stable_id: str, mapped: Sequence[platform_paths.MappedPath], title: str | None
) -> BacklogItem | None:
    """The first candidate that is really here and really readable.

    A candidate with ``resolved is None`` ("unmapped:remote",
    "unmapped:<platform>", "unsafe:share-path") is a real library state with a
    real repair - add a path-map entry - so exhausting every candidate is
    reported as unreachable by the caller rather than dropped.
    """
    for candidate in mapped:
        if candidate.resolved is None:
            continue
        if not fs_residency.is_materialised(candidate.resolved):
            continue
        token = _content_token(candidate.resolved)
        if token is not None:
            return BacklogItem(
                stable_id=stable_id, file_path=str(candidate.resolved),
                title=title, content_token=token,
            )
    return None


#: True where ``st_ctime`` is the inode CHANGE time: the kernel bumps it on
#: every write and userspace cannot set it, which makes it a trustworthy
#: "these bytes moved" bit. POSIX only. On Windows ``st_ctime`` is the file's
#: CREATION time, which a same-length in-place rewrite does not move at all.
CTIME_IS_INODE_CHANGE: bool = os.name != "nt"

#: Read size for :func:`_content_digest`. One MiB keeps a whole track off the
#: heap without paying a syscall per block.
DIGEST_BLOCK_BYTES: int = 1 << 20


def _content_digest(path: Path) -> str:
    """sha256 over the whole file, streamed a block at a time.

    Only reached where stat cannot answer the question (see
    :data:`CTIME_IS_INODE_CHANGE`). Streamed rather than read whole because
    the inputs are audio files and the caller is a timer.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(DIGEST_BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _content_token(
    path: Path, *, ctime_is_inode_change: bool = CTIME_IS_INODE_CHANGE
) -> str | None:
    """A token that changes whenever ``path``'s bytes do. None if it vanished.

    The question this answers is only "are these the same bytes I last failed
    on", so where a cheap answer is a CORRECT answer it takes the cheap one:
    the queue is rescanned on a timer over a whole library, and digesting
    every file to learn nothing would be the wrong cost for that question.

    Where stat is trustworthy (:data:`CTIME_IS_INODE_CHANGE`, i.e. POSIX) the
    token is ``"<size>:<mtime_ns>:<ctime_ns>"``. Size and mtime alone are
    forgeable by the exact tool a repair is likely to use - a
    metadata-preserving rewrite (``cp -p`` over the original, a tag editor
    that stats first and calls ``os.utime`` after) can restore the original
    mtime over completely different bytes of the same length - so ``ctime_ns``
    carries the honesty: the kernel bumps it on every write and userspace
    cannot set it.

    Where stat is NOT trustworthy - Windows, where ``st_ctime`` is creation
    time - the token is ``"<size>:<mtime_ns>:<sha256>"``. A same-length
    in-place repair that restores mtime moves no stat field there whatsoever,
    so a stat-only token would report a repaired file as unchanged, the drain
    would read the queue as unchanged, and a track that had just become
    analyzable would never be retried. Not "unlikely enough to ignore": that
    file is suppressed FOREVER, until some unrelated track happens to move
    the signature. Correctness is worth a read there; it is not worth one
    where ctime already answers.

    ``ctime_is_inode_change`` is a parameter and not a platform sniff inside
    the body so the digest path is reachable, and testable, on the platform
    the tests run on.

    ``None`` when the stat or the read fails: the residency probe ran a moment
    earlier, so a file that has since gone belongs in ``unreachable``, which
    is where the caller puts it. A scan is a snapshot, not a lock.
    """
    try:
        st = path.stat()
        if ctime_is_inode_change:
            return f"{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}"
        return f"{st.st_size}:{st.st_mtime_ns}:{_content_digest(path)}"
    except OSError:
        return None


def _signature(pending: list[BacklogItem]) -> str:
    """Stable digest of the FULL pending set; empty string when nothing pends.

    The auto-drain compares signatures to tell a NEW import from a re-read of
    the same failed queue, so a backend that cannot run never becomes a retry
    storm. It is computed before ``limit`` slices, so a paged read and a full
    read of the same library agree.

    Id, path AND content token are all hashed, because a repair can move any
    one of them while leaving the others alone: a RELINK keeps the stable_id
    and changes the path, an IN-PLACE repair keeps both and changes only the
    bytes. Hashing identity alone would call either repaired queue unchanged
    and never retry an input that had just become analyzable.

    Note this is also what keeps the loop from re-arming itself: the drain
    shells out to ``apps.analysis.run``, which writes analysis ROWS and never
    touches the audio file (writing tags into audio files was removed),
    so a failed drain leaves all three components standing still.
    """
    if not pending:
        return ""
    digest = hashlib.sha256()
    for item in pending:
        for part in (item.stable_id, item.file_path, item.content_token):
            digest.update(part.encode("utf-8"))
            digest.update(b"\x00")
    return digest.hexdigest()


__all__ = [
    "ANLZ_VENDOR",
    "DRAIN_BACKEND",
    "STREAMING_REASON",
    "Backlog",
    "BacklogItem",
    "analyzed_ids",
    "scan",
]
