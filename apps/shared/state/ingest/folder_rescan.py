"""LIBM-128: keep a folder-imported library current while the engine runs.

``ingest_folder`` (this package's ``folder.py``) is one-shot: run it once
from the setup wizard and it never looks again. This module is the part
that looks again, cheaply, on an interval owned by
``apps.engine_core.setup.folder_rescan_scheduler``.

Cheapness rests on one fact about the folder-tier id: :func:`state_ids.stable_id`
mints a folder-imported track's id from ``(abs_path, mtime)`` alone (tier
``"inferred"``, ``isrc`` and ``fingerprint`` both absent) -- it never reads
tags. So "did anything change" is answerable from a stat-only walk
(:func:`folder.collect_audio`, no tag read) plus a pure hash, with a
tag read only for the files that actually turn out new or changed.

Diffing happens in STABLE-ID SPACE, never in raw-path-string space. A tier-3
id already routes every path through ``path_collision_key`` (NFC-normalize +
casefold) before hashing, so two byte-different spellings of one real,
unchanged file hash identically and cancel out of the diff. Comparing raw
``tracks.file_path`` strings instead would flag that file as both removed
and added on every single cycle -- see ``.claude/rules/verification.md``
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
import time
from pathlib import Path
from typing import Any

from apps.shared import audio_files, fs_access
from apps.shared.scan_mass_missing import MassMissingError, guard_roots, path_is_under_root
from apps.shared.state import ids as state_ids
from apps.shared.state.ingest.folder import FolderIngestReport, _write_tracks, collect_audio
from apps.shared.state.ingest.path_collisions import PathCollisionError, assert_no_path_collisions
from apps.shared.state.writer import StateWriter

#: A cycle that finds a change never applies more than this many removals in
#: one pass. ``remove_from_library`` opens its own transaction and publishes
#: one bus event per call, so an unbounded bulk removal (a whole root moved
#: or unplugged mid-scan) would burst that many events onto the WS hub in
#: one round. The remainder waits for the next cycle: bounded, self-healing,
#: and the signature stays unstable (so cheap-skip does not kick in) until
#: the backlog fully drains.
MAX_TOMBSTONES_PER_CYCLE = 250


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

    A path can (rarely) carry more than one live row already -- e.g. a
    pre-existing duplicate from before this reconciler existed. Keeping the
    full list rather than a single id lets the diff below self-heal that
    too: any id at the path that does not match the file's current computed
    id gets tombstoned, not just the last one a dict overwrite would keep.
    """
    rows = conn.execute(
        "SELECT stable_id, file_path FROM tracks "
        "WHERE deleted_at IS NULL AND file_path IS NOT NULL "
        "AND stable_id_tier = 'inferred'"
    ).fetchall()
    by_path: dict[str, list[str]] = {}
    for stable_id, file_path in rows:
        if any(path_is_under_root(file_path, root) for root in roots):
            by_path.setdefault(file_path, []).append(stable_id)
    return by_path


def _initial_scan(
    root_list: list[Path], previous_signature: str
) -> tuple[FolderRescanReport, list[audio_files.AudioFile], bool]:
    """The always-cheap first step: walk, sign, and decide whether to stop.

    Returns ``(report, files, done)``; ``done`` is true for a cheap-skip
    (nothing changed since ``previous_signature``), in which case ``report``
    is already complete except for ``duration_s``.
    """
    files, denied, dataless = collect_audio(root_list)
    signature = compute_signature(files, denied)
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


def _apply_tombstones_and_writes(
    writer: StateWriter,
    report: FolderRescanReport,
    tombstones: list[str],
    write_candidates: list[audio_files.AudioFile],
) -> None:
    """The one transactional step: tombstone, then write, in a SAVEPOINT.

    A SAVEPOINT rather than a bare transaction because ``writer`` may already
    be inside an outer one (``writer_tracks.py``'s own nested-``_tx()``
    contract).
    """
    conn = writer.raw_conn
    savepoint = "folder_rescan_reconcile"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        for stable_id in tombstones:
            writer.remove_from_library(stable_id)
        report.tracks_removed = len(tombstones)

        sub_report = FolderIngestReport(roots=report.roots)
        _write_tracks(writer, write_candidates, sub_report, None)
        report.tracks_added = sub_report.tracks_inserted
        report.tracks_unexpected_noop = sub_report.tracks_unchanged
        if sub_report.tracks_unchanged:
            noop_note = (
                f"{sub_report.tracks_unchanged} write candidate(s) were no-ops; "
                "a stored file_path may not match the live walk (check Unicode "
                "normalization or a stale row outside the configured roots)"
            )
            report.warning = f"{report.warning}; {noop_note}" if report.warning else noop_note
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def reconcile_folders(
    writer: StateWriter,
    roots: list[Path],
    *,
    previous_signature: str,
    allow_mass_missing: bool = False,
) -> FolderRescanReport:
    """One cheap-if-nothing-changed reconcile cycle over ``roots``.

    Additions and content-changes are detected and written; paths no longer
    present, or superseded by a changed-mtime rewrite at the same path, are
    tombstoned via ``writer.remove_from_library``. A ``MassMissingError``
    (a whole root gone missing or mostly missing) and a denied root both
    surface on ``report.warning`` rather than raising -- a background cycle
    has no caller to hand an exception to, so the report IS the failure
    channel.
    """
    start = time.perf_counter()
    root_list = [Path(root).expanduser() for root in roots]
    report, files, done = _initial_scan(root_list, previous_signature)
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

    current_sids = {sid for sid, _tier in current_sid_by_path.values()}
    existing_sids = {sid for sids in existing.values() for sid in sids}
    tombstones = _cap_tombstones(report, sorted(existing_sids - current_sids))
    write_candidates = [
        f for f in files if current_sid_by_path[str(f.path)][0] not in existing_sids
    ]

    _apply_tombstones_and_writes(writer, report, tombstones, write_candidates)

    report.duration_s = round(time.perf_counter() - start, 3)
    return report


__all__ = [
    "MAX_TOMBSTONES_PER_CYCLE",
    "FolderRescanReport",
    "compute_signature",
    "reconcile_folders",
]
