"""LIBM-128: keep a folder-imported library current while the engine runs.

``ingest_folder`` (this package's ``folder.py``) is one-shot: run it once
from the setup wizard and it never looks again. This module is the part
that looks again, cheaply, on an interval owned by
``apps.engine_core.setup.folder_rescan_scheduler``.

Cheapness rests on one fact about the folder-tier id: :func:`state_ids.stable_id`
mints a folder-imported track's id from ``(abs_path, mtime)`` alone (tier
``"inferred"``, ``isrc`` and ``fingerprint`` both absent) -- it never reads
tags. So "did anything change" is answerable from a stat-only walk
(:func:`folder.collect_audio`, no tag-reader call) plus a pure hash, with a
tag read only for the files that actually turn out new or changed.

Diffing happens in STABLE-ID SPACE, never in raw-path-string space. A tier-3
id already routes every path through ``path_collision_key`` (NFC-normalize +
casefold) before hashing, so two byte-different spellings of one real,
unchanged file hash identically and cancel out of the diff. Comparing raw
``tracks.file_path`` strings instead would flag that file as both removed
and added on every single cycle -- see ``(private path)``
"one defect or one instance": normalization drift is exactly the class of
bug a raw-path diff cannot see coming.

A denied root's warning is carried on every report, cheap-skip or not: the
denial itself is folded into the cheap-skip signature (so a permanently
unmounted volume does not force a full table scan forever), but a denied
root always re-surfaces its warning, satisfying "fails loudly, never
silently" without paying the fail-loud cost every cycle.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import time
from pathlib import Path
from typing import Any

from apps.shared import audio_files, fs_access
from apps.shared.scan_mass_missing import MassMissingError, guard_roots, path_is_under_root
from apps.shared.state import ids as state_ids
from apps.shared.state.ingest.folder import (
    MAX_ANALYSIS_DROPS_PER_CYCLE,
    AnalysisDropBudget,
    FolderIngestReport,
    _write_tracks,
    collect_audio,
)
from apps.shared.state.ingest.path_collisions import PathCollisionError, assert_no_path_collisions, path_collision_key
from apps.shared.state.writer import StateWriter
from apps.shared.state.writer_tracks import DELETED_FILE_MISSING

#: A cycle that finds a change never applies more than this many removals in
#: one pass. ``remove_from_library`` opens its own transaction and publishes
#: one bus event per call, so an unbounded bulk removal (a whole root moved
#: or unplugged mid-scan) would burst that many events onto the WS hub in
#: one round. The remainder waits for the next cycle: bounded, self-healing,
#: and the signature stays unstable (so cheap-skip does not kick in) until
#: the backlog fully drains.
MAX_TOMBSTONES_PER_CYCLE = 250

_log = logging.getLogger(__name__)


@dataclasses.dataclass
class FolderRescanReport:
    """What one reconcile cycle found and did. Denominators are named."""

    roots: list[str] = dataclasses.field(default_factory=list)
    unreadable_roots: list[str] = dataclasses.field(default_factory=list)
    files_seen: int = 0
    files_dataless: int = 0
    tracks_added: int = 0
    tracks_removed: int = 0
    tombstones_pending: int = 0
    #: An upsert inside the write phase reported no change for a file this
    #: cycle believed new or changed. That can only mean the pre-write diff
    #: and the writer disagree about what is stored -- surfaced, not folded
    #: silently into ``tracks_added``.
    tracks_unexpected_noop: int = 0
    #: Rewritten rows whose analysis drop was over ``MAX_ANALYSIS_DROPS_PER_CYCLE``.
    #: They were not refreshed and keep their hash and analysis this cycle.
    analysis_drops_withheld: int = 0
    #: Files already in the library as a non-folder row (rekordbox, djay, a
    #: stick import) at the same path. A watched folder that overlaps another
    #: source must not mint a second, folder-tier copy of the same file
    #: (FWATCH-02), so these are counted and left alone.
    files_owned_by_other_source: int = 0
    skipped_no_changes: bool = False
    signature: str = ""
    warning: str | None = None
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def compute_signature(files: list[audio_files.AudioFile], denied: list[str]) -> str:
    """A cheap-skip baseline covering both file state and denied roots.

    Denied roots are folded in so a persistently unmounted or TCC-blocked
    root does not force a full reconcile (and a ``guard_roots`` refusal)
    every single cycle: the signature stays stable while the denial
    persists, and the caller re-derives (and re-surfaces) the warning from
    ``denied`` on every cycle regardless of cheap-skip.
    """
    material = "\n".join(
        sorted(f"{f.path}|{f.size_bytes}|{f.mtime}" for f in files)
    )
    material += "\nDENIED:" + "|".join(sorted(denied))
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


def _existing_live_by_path(conn: Any, roots: list[Path]) -> dict[str, list[str]]:
    """Live ``stable_id``s per ``file_path``, restricted to our own roots.

    Every live row under the roots is in the inventory, including an ISRC
    or fingerprint row the folder writer adopted. A tier filter left that
    row live after its file disappeared. A path can carry more than one
    live row. Keeping the full list lets the diff tombstone each one that
    is gone, not just the last one a dict overwrite would keep.
    """
    rows = conn.execute(
        "SELECT stable_id, file_path FROM tracks "
        "WHERE deleted_at IS NULL AND file_path IS NOT NULL"
    ).fetchall()
    by_path: dict[str, list[str]] = {}
    for stable_id, file_path in rows:
        if any(path_is_under_root(file_path, root) for root in roots):
            by_path.setdefault(file_path, []).append(stable_id)
    return by_path


# A row another source owns: any non-inferred tier, or an inferred row that a
# vendor import (rekordbox, djay, ...) stamped with its own id. A rekordbox
# track with no ISRC is minted ``inferred`` too, so the tier alone misses it.
_OWNED_BY_OTHER_SOURCE = (
    "(t.stable_id_tier != 'inferred' OR EXISTS "
    "(SELECT 1 FROM track_vendor_ids v WHERE v.stable_id = t.stable_id))"
)


def _other_source_path_keys(conn: Any) -> set[str]:
    """Path keys owned by any source other than a folder import.

    A live row owns its path. So does a row the USER removed (``deleted_reason``
    ``user``, or NULL from before the column): a watched folder must not bring
    back, as a folder-tier row, a track the user took out of the library.
    """
    rows = conn.execute(
        "SELECT t.file_path FROM tracks t "
        f"WHERE t.file_path IS NOT NULL AND {_OWNED_BY_OTHER_SOURCE} "
        "AND (t.deleted_at IS NULL OR t.deleted_reason IS NULL OR t.deleted_reason = 'user')"
    ).fetchall()
    return {path_collision_key(file_path) for (file_path,) in rows}


def _other_source_stamp(conn: Any) -> str:
    """One aggregate over non-folder rows, folded into the cheap-skip signature.

    A file skipped as owned by another source (FWATCH-02) must be reconsidered
    once that row is removed or relinked away, even though nothing on disk
    changed. ``COUNT`` plus ``MAX(updated_at)`` moves on any such edit and is
    one query, so an unchanged library still cheap-skips.
    """
    count, latest = conn.execute(
        f"SELECT COUNT(*), MAX(t.updated_at) FROM tracks t WHERE {_OWNED_BY_OTHER_SOURCE}"
    ).fetchone()
    return f"other={count}:{latest}"


def _initial_scan(
    root_list: list[Path], previous_signature: str, other_stamp: str = ""
) -> tuple[FolderRescanReport, list[audio_files.AudioFile], bool]:
    """The always-cheap first step: walk, sign, and decide whether to stop.

    Returns ``(report, files, done)``; ``done`` is true for a cheap-skip
    (nothing changed since ``previous_signature``), in which case ``report``
    is already complete except for ``duration_s``.
    """
    files, denied, dataless = collect_audio(root_list)
    signature = compute_signature(files, denied)
    if other_stamp:
        signature = f"{signature}|{other_stamp}"
    report = FolderRescanReport(
        roots=[str(root) for root in root_list],
        unreadable_roots=denied,
        files_seen=len(files),
        files_dataless=dataless,
        signature=signature,
    )
    if denied:
        report.warning = f"could not read {', '.join(denied)}; {fs_access.GRANT_INSTRUCTIONS}"
    done = signature == previous_signature
    report.skipped_no_changes = done
    return report, files, done


def _collision_warning(files: list[audio_files.AudioFile]) -> str | None:
    try:
        assert_no_path_collisions(str(entry.path) for entry in files)
    except PathCollisionError as exc:
        return str(exc)
    return None


def _mass_missing_warning(
    root_list: list[Path],
    current_sid_by_path: dict[str, tuple[str, str]],
    existing: dict[str, list[str]],
    *,
    allow_mass_missing: bool,
) -> str | None:
    try:
        guard_roots(
            root_list,
            list(current_sid_by_path),
            list(existing),
            allow_mass_missing=allow_mass_missing,
        )
    except MassMissingError as exc:
        return str(exc)
    return None


def _cap_tombstones(report: FolderRescanReport, tombstones: list[str]) -> list[str]:
    """Truncate to the per-cycle cap, and see the ``MAX_TOMBSTONES_PER_CYCLE``
    docstring for why a truncated cycle forces ``report.signature`` empty."""
    report.tombstones_pending = max(0, len(tombstones) - MAX_TOMBSTONES_PER_CYCLE)
    if report.tombstones_pending:
        report.signature = ""
    return tombstones[:MAX_TOMBSTONES_PER_CYCLE]


def _skipped_refresh_note(refresh_report: FolderIngestReport) -> str | None:
    """Count the rewritten files whose refresh did not happen, or None.

    A refresh that is skipped (an unplayable rewrite, an id that cannot be
    minted, a file that is a track the user removed) leaves the row live
    with the hash and analysis of bytes no longer on disk. The count goes on
    the cycle's warning, never dropped with the refresh report. It is a
    count only: the ingest report carries no per-file list.
    """
    skipped = (
        refresh_report.files_rejected_unplayable
        + refresh_report.tracks_skipped
        + refresh_report.tracks_skipped_deleted
    )
    if not skipped:
        return None
    return (
        f"{skipped} rewritten file(s) could not be refreshed "
        f"({refresh_report.files_rejected_unplayable} unplayable); their rows keep "
        "the stored hash and analysis of the previous bytes"
    )


def _report_withheld_drops(report: FolderRescanReport, budget: AnalysisDropBudget) -> None:
    """Put rows the analysis drop bound left unrefreshed on the warning and the log.

    The signature is cleared so the next cycle refreshes them, like a capped tombstone.
    """
    report.analysis_drops_withheld = budget.withheld
    if not budget.withheld:
        return
    report.signature = ""
    note = (
        f"{budget.withheld} rewritten row(s) over the analysis drop bound of "
        f"{budget.limit} per cycle were not refreshed; their analysis is kept "
        "but no longer served until a later cycle"
    )
    _log.warning("folder rescan: %s", note)
    report.warning = f"{report.warning}; {note}" if report.warning else note


def _apply_tombstones_and_writes(
    writer: StateWriter,
    report: FolderRescanReport,
    tombstones: list[str],
    write_candidates: list[audio_files.AudioFile],
    refreshes: list[audio_files.AudioFile],
    drop_budget: AnalysisDropBudget,
) -> None:
    """The one transactional step: tombstone, then write, in one write unit.

    :meth:`StateWriter.transaction` takes the writer lock up front
    (STATE-15) and flushes queued bus events only after COMMIT.

    ``refreshes`` are paths that still exist but whose mtime moved. They are
    written onto the existing row. An unchanged hash is an expected no-op,
    not the path-mismatch warning reserved for brand new candidates.
    """
    with writer._join_or_begin():
        for stable_id in tombstones:
            writer.remove_from_library(stable_id, reason=DELETED_FILE_MISSING)
        report.tracks_removed = len(tombstones)

        sub_report = FolderIngestReport(roots=report.roots)
        _write_tracks(writer, write_candidates, sub_report, None, drop_budget=drop_budget)
        refresh_report = FolderIngestReport(roots=report.roots)
        _write_tracks(writer, refreshes, refresh_report, None, drop_budget=drop_budget)
        _report_withheld_drops(report, drop_budget)
        report.tracks_added = sub_report.tracks_inserted
        report.tracks_unexpected_noop = sub_report.tracks_unchanged
        if sub_report.tracks_unchanged:
            noop_note = (
                f"{sub_report.tracks_unchanged} write candidate(s) were no-ops; "
                "a stored file_path may not match the live walk (check Unicode "
                "normalization or a stale row outside the configured roots)"
            )
            report.warning = f"{report.warning}; {noop_note}" if report.warning else noop_note
        refresh_note = _skipped_refresh_note(refresh_report)
        if refresh_note:
            report.warning = f"{report.warning}; {refresh_note}" if report.warning else refresh_note


def reconcile_folders(
    writer: StateWriter,
    roots: list[Path],
    *,
    previous_signature: str,
    allow_mass_missing: bool = False,
    max_analysis_drops: int = MAX_ANALYSIS_DROPS_PER_CYCLE,
) -> FolderRescanReport:
    """One cheap-if-nothing-changed reconcile cycle over ``roots``.

    Additions are written. Paths no longer present are tombstoned via
    ``writer.remove_from_library``. A changed mtime at a path that still
    exists is written onto that row in this same cycle: the hash and tags
    update, and a changed audio hash drops the old analysis so the drain
    runs the new bytes. The signature saved at the end of the cycle is the
    post-rewrite signature, so the next cycle can cheap-skip. A ``MassMissingError``
    (a whole root gone missing or mostly missing) and a denied root both
    surface on ``report.warning`` rather than raising -- a background cycle
    has no caller to hand an exception to, so the report IS the failure
    channel.
    """
    start = time.perf_counter()
    root_list = [Path(root).expanduser() for root in roots]
    report, files, done = _initial_scan(
        root_list, previous_signature, _other_source_stamp(writer.raw_conn)
    )
    if done:
        report.duration_s = round(time.perf_counter() - start, 3)
        return report

    warning = _collision_warning(files)
    if warning is None:
        conn = writer.raw_conn
        existing = _existing_live_by_path(conn, root_list)
        current_sid_by_path: dict[str, tuple[str, str]] = {
            str(f.path): state_ids.stable_id(isrc=None, abs_path=str(f.path), mtime=f.mtime)
            for f in files
        }
        warning = _mass_missing_warning(
            root_list, current_sid_by_path, existing, allow_mass_missing=allow_mass_missing
        )
    if warning is not None:
        report.warning = warning
        # A refused scan must be evaluated again on the next cycle. Keeping
        # the filesystem signature would cheap-skip the same unsafe state
        # and make the warning disappear from status and logs.
        report.signature = ""
        report.duration_s = round(time.perf_counter() - start, 3)
        return report

    # Diff in the same canonical-path space the tier-3 id uses (NFC +
    # casefold), not raw path strings. A path that is still on disk keeps
    # its live row even when mtime moved. Tombstone only paths that are gone.
    current_keys = {path_collision_key(path) for path in current_sid_by_path}
    existing_by_key: dict[str, list[str]] = {}
    for path, sids in existing.items():
        existing_by_key.setdefault(path_collision_key(path), []).extend(sids)
    gone = [
        sid for key, sids in existing_by_key.items() if key not in current_keys for sid in sids
    ]
    tombstones = _cap_tombstones(report, sorted(set(gone)))
    write_candidates: list[audio_files.AudioFile] = []
    refreshes: list[audio_files.AudioFile] = []
    for entry in files:
        key = path_collision_key(str(entry.path))
        stored = existing_by_key.get(key)
        if not stored:
            write_candidates.append(entry)
            continue
        minted = current_sid_by_path[str(entry.path)][0]
        if minted not in stored:
            refreshes.append(entry)
    other_source = _other_source_path_keys(writer.raw_conn)
    owned = [f for f in write_candidates if path_collision_key(str(f.path)) not in other_source]
    report.files_owned_by_other_source = len(write_candidates) - len(owned)
    write_candidates = owned

    _apply_tombstones_and_writes(
        writer, report, tombstones, write_candidates, refreshes,
        AnalysisDropBudget(limit=max_analysis_drops),
    )

    report.duration_s = round(time.perf_counter() - start, 3)
    return report


__all__ = [
    "MAX_ANALYSIS_DROPS_PER_CYCLE",
    "MAX_TOMBSTONES_PER_CYCLE",
    "FolderRescanReport",
    "compute_signature",
    "reconcile_folders",
]
