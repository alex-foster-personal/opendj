"""One service behind every ANALYSE-ME surface (HTTP, MCP, CLI): plan, apply, adopt.

``plan`` is read-only and safe any time. ``apply`` re-plans from fresh snapshots
and refuses unless the result is exactly the plan the caller confirmed (by
``plan_sha``), so what was shown is what gets written. ``adopt`` brings
Rekordbox's analysis into Open DJ through the existing read-only Rekordbox
ingest.

-Claude
"""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from apps.rekordbox_analyse.plan import AnalysePlan, OdjTrack, build_plan, normalise_path
from apps.rekordbox_analyse.snapshot import RbSnapshot, copy_with_sidecars, snapshot_open_dj, snapshot_rekordbox
from apps.rekordbox_analyse.writer import ApplyReport, apply_plan, running_rekordbox_processes
from apps.shared import platform_paths
from apps.shared.paths import DATA_DIR, STATE_DB
from apps.shared.rekordbox_db import decrypt_to_plain, is_plain_sqlite

BACKUPS_ROOT = DATA_DIR / "writeback-backups" / "rekordbox-analyse"


@dataclass(frozen=True)
class Locations:
    live_db: Path
    state_db: Path
    backups_root: Path


def locations() -> Locations:
    """Where the live Rekordbox library, Open DJ's state and the backups are, read at call time."""
    return Locations(platform_paths.REKORDBOX_LIVE_DB, STATE_DB, BACKUPS_ROOT)


def _in_scope(tracks: tuple[OdjTrack, ...], prefixes: tuple[str, ...]) -> tuple[OdjTrack, ...]:
    """Only tracks in an Open DJ playlist whose name starts with one of ``prefixes``; all tracks when empty."""
    if not prefixes:
        return tracks
    return tuple(t for t in tracks if any(name.startswith(prefixes) for name in t.playlists))


def make_plan(where: Locations, prefixes: tuple[str, ...] = ()) -> tuple[AnalysePlan, RbSnapshot]:
    rb = snapshot_rekordbox(where.live_db)
    odj = _in_scope(snapshot_open_dj(where.state_db), prefixes)
    plan = build_plan(odj, rb.tracks, rb.playlists, rb.master_db_sha256)
    return plan, rb


def plan_status(where: Locations, prefixes: tuple[str, ...] = ()) -> dict[str, object]:
    """The plan plus whether a write could run right now (Rekordbox quit or not)."""
    plan, _ = make_plan(where, prefixes)
    return {**plan.to_dict(), "rekordbox_running": running_rekordbox_processes()}


def apply(
    plan_sha: str, *, cloud_sync_off_confirmed: bool, where: Locations, prefixes: tuple[str, ...] = ()
) -> ApplyReport:
    plan, rb = make_plan(where, prefixes)
    return apply_plan(
        plan,
        plan_sha=plan_sha,
        live_db=where.live_db,
        backups_root=where.backups_root,
        before=rb,
        cloud_sync_off_confirmed=cloud_sync_off_confirmed,
    )


def _scope_by_path(where: Locations, prefixes: tuple[str, ...]) -> dict[str, str]:
    """NFC path -> Open DJ stable_id for live in-scope tracks."""
    return {
        normalise_path(t.file_path): t.stable_id
        for t in _in_scope(snapshot_open_dj(where.state_db), prefixes)
        if t.file_path
    }


def adopt(where: Locations, prefixes: tuple[str, ...] = (), *, dry_run: bool = False) -> dict[str, object]:
    """Bring Rekordbox's BPM, key and cues into the EXISTING Open DJ tracks (RBXAN-8322i).

    Never inserts or updates a track row and never touches playlists: the
    library-wide ingest overwrites title, artists, album, ISRC and duration
    with Rekordbox's (empty for rows ANALYSE-ME added) and mints new ids for
    ISRC rows, so it is not used. Matching is by NFC path to a live in-scope
    Open DJ track; only Rekordbox rows that are analysed count. Rating is not
    adopted (rows we added carry none). Cues follow ``cue_store`` rules (an
    Open DJ-edited cue set is never overwritten). The grid is RBXAN-8322g.
    """
    from pyrekordbox import Rekordbox6Database

    from apps.shared.state import cue_store
    from apps.shared.state import db as state_db
    from apps.shared.state.ingest.rekordbox import _rb_cues_by_content, _rb_modified_at, _rb_rows
    from apps.shared.state.writer import StateWriter

    by_path = _scope_by_path(where, prefixes)
    counts = {"matched": 0, "bpm": 0, "key": 0, "cue_sets": 0, "vendor_links": 0}
    with tempfile.TemporaryDirectory(prefix="rbx-analyse-adopt-") as tmp:
        copy = copy_with_sidecars(where.live_db, Path(tmp))
        plain = copy if is_plain_sqlite(copy) else decrypt_to_plain(copy, Path(tmp) / "master.plain.db")
        analysed = {
            str(cid)
            for (cid,) in sqlite3.connect(f"file:{plain}?mode=ro", uri=True).execute(
                "SELECT ID FROM djmdContent WHERE COALESCE(Analysed, 0) != 0 AND COALESCE(AnalysisDataPath, '') != ''"
            )
        }
        rb = Rekordbox6Database(path=str(plain), unlock=False)
        try:
            rows = [r for r in _rb_rows(rb) if r["id"] in analysed and normalise_path(r["folder_path"]) in by_path]
            cues = _rb_cues_by_content(rb)
        finally:
            rb.close()
            rb.engine.dispose()
    counts["matched"] = len(rows)
    if dry_run:
        counts["bpm"] = sum(1 for r in rows if r["bpm"])
        counts["key"] = sum(1 for r in rows if r["key_name"])
        return {"adopted": False, **counts}
    conn = state_db.open_rw(where.state_db)
    writer = StateWriter(conn, actor="rekordbox-analyse-adopt")
    try:
        for row in rows:
            sid = by_path[normalise_path(row["folder_path"])]
            modified_at = _rb_modified_at(row["updated_at"])
            writer.set_vendor_id(sid, "rekordbox", row["id"])
            counts["vendor_links"] += 1
            if row["bpm"] and writer.set_field(sid, "bpm", row["bpm"], source="rekordbox", modified_at=modified_at):
                counts["bpm"] += 1
            if row["key_name"] and writer.set_field(
                sid, "key", row["key_name"], source="rekordbox", modified_at=modified_at
            ):
                counts["key"] += 1
            counts["cue_sets"] += int(
                cue_store.import_vendor_cues(
                    writer.set_field, conn, sid, cues.get(row["id"], []), source="rekordbox", modified_at=modified_at
                )
            )
    finally:
        writer.close()
        conn.close()
    return {"adopted": True, **counts}


def run_round(where: Locations, prefixes: tuple[str, ...], *, cloud_sync_off_confirmed: bool) -> dict[str, object]:
    """One queue round, every step in order so none is forgotten.

    1. adopt what Rekordbox has analysed (fields and cues only, existing tracks only);
    2. plan again; 3. write it if there is work and Rekordbox is quit, else say why;
    4. say what is left for the maintainer (click Analyze on ANALYSE-ME, quit Rekordbox).
    """
    steps: dict[str, object] = {}
    steps["adopt"] = adopt(where, prefixes)
    plan, rb = make_plan(where, prefixes)
    steps["plan"] = {k: plan.to_dict()[k] for k in ("waiting", "add_deferred", "skipped", "viva_status", "is_noop")}
    running = running_rekordbox_processes()
    if plan.is_noop:
        steps["apply"] = "nothing to write"
    elif running:
        steps["apply"] = f"skipped: quit {', '.join(running)} and run again"
    else:
        steps["apply"] = apply_plan(
            plan,
            plan_sha=plan.plan_sha,
            live_db=where.live_db,
            backups_root=where.backups_root,
            before=rb,
            cloud_sync_off_confirmed=cloud_sync_off_confirmed,
        ).to_dict()
    waiting = plan.waiting
    nxt = (
        f"the maintainer: open Rekordbox, select ANALYSE-ME ({waiting} tracks), select all, Analyze, quit; then run again"
        if waiting
        else "nothing waiting for Rekordbox"
    )
    return {"ok": True, "steps": steps, "next": nxt}


__all__ = [
    "BACKUPS_ROOT",
    "Locations",
    "adopt",
    "apply",
    "locations",
    "make_plan",
    "plan_status",
    "run_round",
]
