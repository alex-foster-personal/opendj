"""``track_availability``: availability as an explicit dimension.

the maintainer's ask, Tue 28 Jul 2026: "can we still grab the analyses of the files we
don't have but mark them file missing? maybe in a different table to prevent
confusion later, doubt db schema will change much."

The second half is the constraint that shaped this. For a track that HAS a
``tracks`` row, the analysis does NOT move to a second table. Copying analysis
into an "orphan" table creates a migration that rots the instant the file comes
back: two rows for one truth, and every consumer has to remember to check both.
Instead the MISSING-ness becomes its own dimension, one row per stable_id, and
the confusion is prevented by making the safe query the easy one:

* ``tracks``            -- every row, playable or not. Use only deliberately.
* ``tracks_available``  -- audio confirmed on disk. The default for aggregates.
* ``tracks_unavailable``-- everything else, carrying its state and check time.

A track with NO ``track_availability`` row is UNKNOWN, and is excluded from
``tracks_available``. Absence of evidence is not evidence of presence.

States:

==================  =========================================================
state               meaning
==================  =========================================================
``present``         ``file_path`` resolves to an existing file right now
``absent``          an absolute path that does not resolve, on a mounted
                    volume. The file is genuinely gone (the dead
                    ``/Users/old`` home is 6,153 of these)
``awaiting_volume`` path is under ``/Volumes/<name>`` and that volume is not
                    mounted. NOT the same as absent: plug the drive in and it
                    is present again, so a relocate pass must not touch it
``streaming``       not a local file at all (``soundcloud:tracks:123...``).
                    There is no file to be missing
==================  =========================================================

Measured on the real ``state.db`` Tue 28 Jul 2026: 1,188 present, 7,155
absent, 11 awaiting_volume, 1 streaming, of 8,355 rows.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from apps.shared.state.availability_write import (
    AvailabilityReport,
    AvailabilityRow,
    guard_present_drop,
)
from apps.shared.state.events import FakeEventBus
from apps.shared.state.locations import list_location_paths
from apps.shared.state.writer import StateWriter

VOLUMES_ROOT = "/Volumes"


def _mounted_volumes() -> set[str]:
    """Names under ``/Volumes``. Empty set if the directory is unreadable."""
    try:
        return set(os.listdir(VOLUMES_ROOT))
    except OSError:
        return set()


def _looks_like_uri(path: str) -> bool:
    """True for ``scheme:rest`` with a non-empty alphabetic scheme.

    ``soundcloud:tracks:1104342268`` is a streaming id, not a path. A Windows
    drive letter (``C:\\...``) is deliberately NOT treated as a URI: a scheme
    must be 2+ characters.
    """
    head, sep, rest = path.partition(":")
    if not sep or not rest:
        return False
    return len(head) >= 2 and head.replace("+", "").replace("-", "").isalnum() and (
        not head[0].isdigit()
    )


def classify_path(
    file_path: str | None, *, mounted: set[str] | None = None
) -> tuple[str, str | None]:
    """Classify one ``tracks.file_path``. Returns ``(state, checked_path)``.

    Pure apart from the ``os.path.exists`` probe, so it is unit-testable
    against a tmp_path without touching the real library.
    """
    if file_path is None or not file_path.strip():
        return "absent", None
    path = file_path.strip()
    if not path.startswith("/") and _looks_like_uri(path):
        return "streaming", path
    if os.path.exists(path):
        return "present", path
    if path.startswith(VOLUMES_ROOT + "/"):
        remainder = path[len(VOLUMES_ROOT) + 1 :]
        volume = remainder.split("/", 1)[0]
        names = _mounted_volumes() if mounted is None else mounted
        if volume and volume not in names:
            return "awaiting_volume", path
    return "absent", path


def _classify_track_row(
    stable_id: str,
    file_path: str | None,
    alt_paths: dict[str, list[str]],
    *,
    mounted: set[str],
) -> AvailabilityRow:
    state, checked = classify_path(file_path, mounted=mounted)
    if state != "present":
        awaiting: tuple[str, str | None] | None = None
        for alt in alt_paths.get(stable_id, []):
            alt_state, alt_checked = classify_path(alt, mounted=mounted)
            if alt_state == "present":
                state, checked = alt_state, alt_checked
                awaiting = None
                break
            if alt_state == "awaiting_volume" and awaiting is None:
                awaiting = (alt_state, alt_checked)
        if state == "absent" and awaiting is not None:
            state, checked = awaiting
    return AvailabilityRow(stable_id=stable_id, state=state, checked_path=checked)


def probe_batch(
    conn: sqlite3.Connection, stable_ids: list[str]
) -> list[AvailabilityRow]:
    """Classify a batch of live tracks. Read-only; does not write."""
    if not stable_ids:
        return []
    mounted = _mounted_volumes()
    placeholders = ",".join("?" * len(stable_ids))
    tracks = list(
        conn.execute(
            f"SELECT stable_id, file_path FROM tracks "
            f"WHERE deleted_at IS NULL AND stable_id IN ({placeholders}) "
            f"ORDER BY stable_id",
            stable_ids,
        )
    )
    alt_paths = list_location_paths(conn, [stable_id for stable_id, _ in tracks])
    return [
        _classify_track_row(stable_id, file_path, alt_paths, mounted=mounted)
        for stable_id, file_path in tracks
    ]


def probe(conn: sqlite3.Connection) -> list[AvailabilityRow]:
    """Classify every ``tracks`` row. Read-only; does not write.

    ``tracks.file_path`` is the legacy primary path, but a track can also
    have local ``track_locations`` rows (a relocated copy, another folder)
    that the audio endpoint's shared location picker
    (:func:`apps.shared.state.locations.pick_playable`) already treats as
    playable through this machine. When the legacy path does not resolve,
    fall back to this machine's alternate locations before calling the
    track absent or awaiting_volume, so a genuinely playable track is not
    excluded from the safe-default availability views.
    """
    stable_ids = [
        row[0]
        for row in conn.execute(
            "SELECT stable_id FROM tracks WHERE deleted_at IS NULL ORDER BY stable_id"
        )
    ]
    return probe_batch(conn, stable_ids)


def write(
    conn: sqlite3.Connection,
    rows: list[AvailabilityRow],
    *,
    now: str | None = None,
    allow_mass_missing: bool = False,
    apply_mass_missing_guard: bool = True,
    always_refresh_checked_at: bool = False,
) -> AvailabilityReport:
    """Upsert ``rows`` into ``track_availability``. Idempotent.

    By default (``always_refresh_checked_at=False``), a row whose state and
    path are unchanged keeps its original ``checked_at``, so the timestamp
    answers "when did this state last change" rather than "when did the
    probe last run". Callers that re-select rows off ``checked_at`` staleness
    (the engine's background worker) pass ``always_refresh_checked_at=True``
    so a re-probed row that comes back unchanged still settles instead of
    being re-selected as stale on every future pass.

    LIBM-41: a library that was present and is now empty (or dropped by more
    than 50%) is refused unless ``allow_mass_missing`` is set. The check
    runs before any row is written when ``apply_mass_missing_guard`` is True.
    """
    with StateWriter(conn, bus=FakeEventBus()) as writer:
        return writer.upsert_availability(
            rows,
            now=now,
            allow_mass_missing=allow_mass_missing,
            apply_mass_missing_guard=apply_mass_missing_guard,
            always_refresh_checked_at=always_refresh_checked_at,
        )


def refresh(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
    allow_mass_missing: bool = False,
) -> AvailabilityReport:
    """Probe then write in one call."""
    return write(
        conn, probe(conn), now=now, allow_mass_missing=allow_mass_missing
    )


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Current ``track_availability`` histogram, plus ``unknown``.

    Joined to ``tracks`` and filtered to ``deleted_at IS NULL``: a
    soft-deleted track's ``track_availability`` row outlives the tombstone,
    so an unfiltered histogram counts it while ``total`` counts only live
    tracks, and ``unknown`` can go negative.
    """
    out = {
        state: count
        for state, count in conn.execute(
            "SELECT track_availability.state, COUNT(*) FROM track_availability "
            "JOIN tracks ON tracks.stable_id = track_availability.stable_id "
            "WHERE tracks.deleted_at IS NULL "
            "GROUP BY track_availability.state"
        )
    }
    total = conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL"
    ).fetchone()[0]
    out["unknown"] = total - sum(out.values())
    return out


def resolve_data_dir(raw: str | None) -> Path:
    """``--data-dir`` resolver. Explicit failure beats a silent wrong root."""
    if raw is None:
        from apps.shared.paths import DATA_DIR

        return Path(DATA_DIR)
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise NotADirectoryError(f"--data-dir {path} is not a directory")
    return path


__all__ = [
    "VOLUMES_ROOT",
    "AvailabilityReport",
    "AvailabilityRow",
    "classify_path",
    "counts",
    "guard_present_drop",
    "probe",
    "probe_batch",
    "refresh",
    "resolve_data_dir",
    "write",
]
