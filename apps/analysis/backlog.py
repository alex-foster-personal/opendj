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
  3. ``too_long``    - the library's own duration is over the analyzer's
     ``max_track_minutes`` cap, so the drain would answer ``TrackTooLong``
     for it every time and end every run in ``error`` (NATIVE-38). Counted
     by name, never queued.
  4. ``pending_total`` - the work queue. ``analyzed + unreachable +
     too_long + pending_total`` always equals ``unmapped``.

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
    [if] a track's stored duration is over the analyzer cap [then] it counts
      too_long and is never pending; an unknown duration stays pending
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from apps.shared import fs_residency, platform_paths
from apps.shared.state import analysis_stale
from apps.shared.state import locations as track_locations
from apps.shared.state import sync_stamp as _sync_stamp

from . import config as analysis_config
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
SELECT t.stable_id, t.file_path, t.title, t.duration_ms
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
    """One queued track. ``file_path`` is the playback winner the runner tries first.

    ``candidate_paths`` and ``candidate_tokens`` carry every materialized local
    copy in playback order for decode fallback and for the change signature.
    ``content_token`` is the token for ``file_path`` alone, kept for callers
    that only read the winner.
    """

    stable_id: str
    file_path: str
    title: str | None
    content_token: str
    candidate_paths: tuple[str, ...] = ()
    candidate_tokens: tuple[str, ...] = ()


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
    too_long: int = 0


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
    # A row retired by an audio rewrite describes other bytes: not analyzed,
    # the same answer run.filter_missing gives.
    retired = analysis_stale.retired_rows(conn, backend)
    return {
        row[0] for row in conn.execute(
            "SELECT stable_id, backend_version FROM analysis WHERE backend = ?", (backend,)
        )
        if (row[0], row[1]) not in retired
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
    cap_ms = analyzer_cap_ms()
    path_map = platform_paths.load_path_map()
    # NATIVE-8340: one machine-id resolution per scan, not one per track (a
    # PRAGMA, a realpath and an id-file read each; 2954 times per GET
    # /analysis-queue on the maintainer's library took the scan from 0.05 s to 0.30 s).
    machine_id = _sync_stamp.local_machine_id(conn)
    pending: list[BacklogItem] = []
    unmapped = 0
    analyzed = 0
    unreachable = 0
    too_long = 0
    rows = conn.execute(_UNMAPPED_SQL, {"anlz_vendor": ANLZ_VENDOR}).fetchall()
    locations = _local_location_paths(conn, [sid for sid, _, _, _ in rows])
    for stable_id, file_path, title, duration_ms in rows:
        if not _has_local_non_streaming_candidate(
            file_path, locations.get(stable_id, ()), path_map=path_map
        ):
            continue
        unmapped += 1
        if stable_id in done:
            analyzed += 1
            continue
        if cap_ms is not None and duration_ms is not None and duration_ms > cap_ms:
            too_long += 1
            continue
        item = _pending_item(
            conn, stable_id, title, path_map=path_map, machine_id=machine_id,
        )
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
        too_long=too_long,
    )


def analyzer_cap_ms() -> int | None:
    """The drain backend's ``max_track_minutes`` in ms, or None when uncapped (0)."""
    minutes = float(analysis_config.load_config()["analyzer"]["max_track_minutes"])
    return int(minutes * 60_000) if minutes > 0 else None


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


def _has_local_non_streaming_candidate(
    legacy: str | None,
    location_paths: Sequence[str],
    *,
    path_map: platform_paths.PathMap | None,
) -> bool:
    """True when the track names at least one non-streaming local path."""
    for raw in (legacy, *location_paths):
        if not raw:
            continue
        mapped = platform_paths.resolve_library_path(raw, path_map=path_map)
        if mapped.reason != STREAMING_REASON:
            return True
    return False


def _pending_item(
    conn: sqlite3.Connection,
    stable_id: str,
    title: str | None,
    *,
    path_map: platform_paths.PathMap | None,
    machine_id: str,
) -> BacklogItem | None:
    """Build a pending item from the playback-ordered materialized candidates."""
    picks = track_locations.ordered_playable_candidates(
        conn,
        stable_id,
        path_map=path_map,
        for_analysis=True,
        machine_id=machine_id,
    )
    if not picks:
        return None
    paths: list[str] = []
    tokens: list[str] = []
    for pick in picks:
        token = _content_token(pick.path)
        if token is None:
            continue
        paths.append(str(pick.path))
        tokens.append(token)
    if not paths:
        return None
    return BacklogItem(
        stable_id=stable_id,
        file_path=paths[0],
        title=title,
        content_token=tokens[0],
        candidate_paths=tuple(paths),
        candidate_tokens=tuple(tokens),
    )


def _candidate_paths(
    legacy: str | None, location_paths: Sequence[str]
) -> list[str]:
    """Legacy-first path list retained for tests and explicit backfill resolution."""
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
    """First materialized candidate from a legacy-ordered mapped list."""
    for candidate in mapped:
        if candidate.resolved is None:
            continue
        if not fs_residency.is_materialised(candidate.resolved):
            continue
        token = _content_token(candidate.resolved)
        if token is not None:
            path = str(candidate.resolved)
            return BacklogItem(
                stable_id=stable_id,
                file_path=path,
                title=title,
                content_token=token,
                candidate_paths=(path,),
                candidate_tokens=(token,),
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
        paths = item.candidate_paths or (item.file_path,)
        tokens = item.candidate_tokens or (item.content_token,)
        for path, token in zip(paths, tokens, strict=True):
            for part in (item.stable_id, path, token):
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
