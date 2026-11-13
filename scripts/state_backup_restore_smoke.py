"""Disposable state.db backup and full-file restore drill (issue #12952).

A verified backup alone is not recovery evidence. This module runs a throwaway
drill: seed ``state.db``, take the production authoritative backup path, replace
the live file from that backup, then read and write through the real state layer.

Never touches caller data unless ``--data-dir`` is passed explicitly.

    python -m scripts.state_backup_restore_smoke
    python -m scripts.state_backup_restore_smoke --data-dir /tmp/disposable-data
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from apps.shared import sqlite_verified_copy as svc
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.shared.state_authoritative_backup import StateAuthoritativeBackupError, backup_state_db

SEED_STABLE_ID = "platform-smoke-seed"
POST_RESTORE_STABLE_ID = "platform-smoke-post-restore"
SEED_TITLE = "Smoke Seed Track"
POST_RESTORE_TITLE = "Smoke Post-Restore Track"


class StateBackupRestoreSmokeError(RuntimeError):
    """The disposable backup/restore drill could not complete."""


@dataclass(frozen=True)
class SmokeReport:
    data_dir: str
    backup_path: str
    seed_stable_id: str
    post_restore_stable_id: str
    backup_row_counts: dict[str, int]
    restored_row_counts: dict[str, int]


def _state_path(data_dir: Path) -> Path:
    return data_dir / "state" / "state.db"


def _unlink_state_files(state_path: Path) -> None:
    for suffix in svc.VerifiedCopyCFG.SIDECAR_SUFFIXES:
        Path(f"{state_path}{suffix}").unlink(missing_ok=True)


def seed_disposable_library(data_dir: Path) -> None:
    """Write one track through the production writer into a fresh migrated state.db."""
    state_path = _state_path(data_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    conn = state_db.open_rw(state_path)
    try:
        writer = StateWriter(conn)
        writer.upsert_track(
            stable_id=SEED_STABLE_ID,
            stable_id_tier="inferred",
            title=SEED_TITLE,
            artists=["Smoke Artist"],
            album=None,
            isrc=None,
            duration_ms=180_000,
            file_path=None,
        )
        conn.commit()
    finally:
        conn.close()


def backup_disposable_library(data_dir: Path, *, now: datetime | None = None) -> Path:
    """Take one verified authoritative backup; return the finished backup path."""
    dest = data_dir / "backup" / "smoke-restore"
    try:
        report = backup_state_db(data_dir, dest_dir=dest, keep=3, now=now)
    except StateAuthoritativeBackupError as exc:
        raise StateBackupRestoreSmokeError(str(exc)) from exc
    return report.path


def wipe_live_state_db(data_dir: Path) -> None:
    """Replace the live database with an empty migrated file (simulated loss)."""
    state_path = _state_path(data_dir)
    _unlink_state_files(state_path)
    conn = state_db.open_rw(state_path)
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()
        if count != 0:
            raise StateBackupRestoreSmokeError("fresh wipe left rows in tracks")
        conn.commit()
    finally:
        conn.close()


def restore_full_state_db(live: Path, backup: Path) -> dict[str, int]:
    """Replace ``live`` and its sidecars from a verified backup file."""
    try:
        counts = svc.verify_backup(backup)
    except svc.VerifiedCopyError as exc:
        raise StateBackupRestoreSmokeError(str(exc)) from exc
    _unlink_state_files(live)
    live.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, live)
    try:
        restored = svc.verify_backup(live)
    except svc.VerifiedCopyError as exc:
        raise StateBackupRestoreSmokeError(str(exc)) from exc
    svc.require_equal_counts(counts, restored, f"restore of {live}")
    return restored


def read_seed_track(data_dir: Path) -> str:
    """Query the seed track title through :func:`state_db.open_rw`."""
    conn = state_db.open_rw(_state_path(data_dir))
    try:
        row = conn.execute(
            "SELECT title FROM tracks WHERE stable_id = ?",
            (SEED_STABLE_ID,),
        ).fetchone()
        if row is None:
            raise StateBackupRestoreSmokeError(f"missing seed track {SEED_STABLE_ID!r}")
        return str(row[0])
    finally:
        conn.close()


def write_post_restore_track(data_dir: Path) -> None:
    """Append one track after restoration through :func:`StateWriter`."""
    conn = state_db.open_rw(_state_path(data_dir))
    try:
        writer = StateWriter(conn)
        writer.upsert_track(
            stable_id=POST_RESTORE_STABLE_ID,
            stable_id_tier="inferred",
            title=POST_RESTORE_TITLE,
            artists=["Smoke Artist"],
            album=None,
            isrc=None,
            duration_ms=200_000,
            file_path=None,
        )
        conn.commit()
    finally:
        conn.close()


def read_post_restore_track(data_dir: Path) -> str:
    conn = state_db.open_rw(_state_path(data_dir))
    try:
        row = conn.execute(
            "SELECT title FROM tracks WHERE stable_id = ?",
            (POST_RESTORE_STABLE_ID,),
        ).fetchone()
        if row is None:
            raise StateBackupRestoreSmokeError(f"missing post-restore track {POST_RESTORE_STABLE_ID!r}")
        return str(row[0])
    finally:
        conn.close()


def run_drill(data_dir: Path, *, now: datetime | None = None) -> SmokeReport:
    """Run backup, wipe, full-file restore, read, and write on a disposable data dir."""
    data_dir = Path(data_dir)
    seed_disposable_library(data_dir)
    backup_path = backup_disposable_library(data_dir, now=now)
    backup_counts = svc.verify_backup(backup_path)
    wipe_live_state_db(data_dir)
    live = _state_path(data_dir)
    restored_counts = restore_full_state_db(live, backup_path)
    title = read_seed_track(data_dir)
    if title != SEED_TITLE:
        raise StateBackupRestoreSmokeError(f"restored seed title {title!r} != {SEED_TITLE!r}")
    write_post_restore_track(data_dir)
    post_title = read_post_restore_track(data_dir)
    if post_title != POST_RESTORE_TITLE:
        raise StateBackupRestoreSmokeError(f"post-restore title {post_title!r} != {POST_RESTORE_TITLE!r}")
    return SmokeReport(
        data_dir=str(data_dir),
        backup_path=str(backup_path),
        seed_stable_id=SEED_STABLE_ID,
        post_restore_stable_id=POST_RESTORE_STABLE_ID,
        backup_row_counts=backup_counts,
        restored_row_counts=restored_counts,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Disposable data directory (default: a new temp dir)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the drill report as JSON on success",
    )
    args = parser.parse_args(argv)
    owns_temp = args.data_dir is None
    data_dir = args.data_dir
    if data_dir is None:
        data_dir = Path(tempfile.mkdtemp(prefix="state-backup-restore-smoke-"))
    try:
        report = run_drill(data_dir, now=datetime.now(UTC))
    except StateBackupRestoreSmokeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(asdict(report), sort_keys=True))
    else:
        print(f"ok: backup={report.backup_path} data_dir={report.data_dir}")
    if owns_temp:
        shutil.rmtree(data_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
