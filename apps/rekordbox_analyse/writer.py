"""Guarded direct write of an ANALYSE-ME plan into Rekordbox (RBXAN-8322b).

Mimics Lexicon's Direct Sync guards and adds three it does not document
(spec ``(private path)``). In order:

1. one-way gate (``MDT_REKORDBOX_WRITEBACK_ENABLED``), surface
   ``module.rekordbox_analyse.apply``;
2. the caller confirms Rekordbox Cloud Sync is off (fail closed until the
   airgap test measures what Cloud Sync does to a direct write);
3. neither ``rekordbox`` nor ``rekordboxAgent`` is running (never killed);
4. the plan digest matches, and ``master.db`` still has the plan's sha256;
5. every top-level file of the Rekordbox folder is copied aside (``share/``,
   the ANLZ tree, is left out: this write never touches it and it is GBs);
6. one pyrekordbox session, one ``commit()``;
7. read back a fresh decrypted copy: every planned row and membership is
   there, no other track appeared or vanished, ``PRAGMA integrity_check`` is ok;
8. any failure after step 5 copies the backup back and says so.

-Claude
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from apps.rekordbox_analyse.plan import (
    ANALYSE_ME_PLAYLIST,
    VIVA_TARGET_PLAYLIST,
    AnalysePlan,
    normalise_path,
    playlists_by_name,
)
from apps.rekordbox_analyse.snapshot import (
    RbSnapshot,
    copy_with_sidecars,
    integrity_ok,
    read_plain_rekordbox,
    sha256_file,
)
from apps.shared.rekordbox_db import decrypt_to_plain, is_plain_sqlite
from apps.shared.rekordbox_writeback import require_writeback_enabled

SURFACE_ID = "module.rekordbox_analyse.apply"
REKORDBOX_PROCESSES: tuple[str, ...] = ("rekordbox", "rekordboxAgent")
KEEP_BACKUPS = 10


# -----------------------------------------------------------------------------
# Errors


class AnalyseApplyRefused(RuntimeError):
    """A guard refused before anything was written. Nothing changed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


class AnalyseApplyFailed(RuntimeError):
    """The write or its verification failed after the backup; the backup was restored."""

    def __init__(self, message: str, *, backup_dir: Path, restored: bool) -> None:
        self.backup_dir = backup_dir
        self.restored = restored
        super().__init__(message)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": "apply_failed_restored" if self.restored else "apply_failed_restore_failed",
            "message": str(self),
            "backup_dir": str(self.backup_dir),
            "restored": self.restored,
        }


@dataclass(frozen=True)
class ApplyReport:
    backup_dir: str
    added: int
    playlist_added: int
    playlist_removed: int
    viva_added: int
    master_db_sha256_after: str
    integrity: str
    pruned_backups: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


# -----------------------------------------------------------------------------
# Guards


def _normalise_process_name(raw: object) -> str:
    """Basename, case-insensitive, with one trailing Windows ``.exe`` stripped."""
    return Path(str(raw).replace("\\", "/")).name.casefold().removesuffix(".exe")


def running_rekordbox_processes(
    names: Sequence[str] = REKORDBOX_PROCESSES,
    *,
    process_iter: Callable[..., Any] | None = None,
) -> list[str]:
    """Exact, case-insensitive process-name matches, including a Windows ``.exe`` suffix.

    A missing enumerator is an error, not 'none running'. Never kills. Each
    requested name is returned at most once, in ``names`` order.
    """
    enumerator = process_iter or psutil.process_iter
    try:
        iterator = enumerator(["pid", "name"])
    except Exception as exc:
        raise RuntimeError(f"cannot enumerate processes: {exc}") from exc

    live: set[str] = set()
    try:
        for proc in iterator:
            try:
                info = getattr(proc, "info", None) or {}
                raw = info.get("name") if isinstance(info, dict) else None
                if not raw:
                    name_fn = getattr(proc, "name", None)
                    raw = name_fn() if callable(name_fn) else None
            except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
                continue
            if raw:
                live.add(_normalise_process_name(raw))
    except Exception as exc:
        raise RuntimeError(f"cannot enumerate processes: {exc}") from exc

    found: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        if _normalise_process_name(name) in live:
            found.append(name)
            seen.add(name)
    return found


@contextmanager
def _exclusive_target_lock(target: Path):
    """Serialize ANALYSE-ME apply with playlist writeback on the same library.

    Lock path must stay aligned with ``apps.smartlists.writeback_backup.exclusive_target_lock``
    so playlist writeback and ANALYSE-ME apply cannot overlap on one ``master.db``.
    """
    lock = target.with_name(f".{target.name}.writeback.lock")
    try:
        descriptor = lock.open("x")
    except FileExistsError as exc:
        raise AnalyseApplyRefused(
            "writeback_in_progress",
            f"writeback target is already owned: {target}",
        ) from exc
    try:
        yield
    finally:
        descriptor.close()
        lock.unlink(missing_ok=True)


def _check_guards(
    plan: AnalysePlan,
    *,
    live_db: Path,
    plan_sha: str,
    cloud_sync_off_confirmed: bool,
    process_check: Callable[[], list[str]],
) -> None:
    require_writeback_enabled("module.rekordbox_analyse.apply")
    if not cloud_sync_off_confirmed:
        raise AnalyseApplyRefused(
            "cloud_sync_unconfirmed",
            "Confirm Rekordbox Cloud Sync is off; direct writes are not yet tested with it on",
        )
    running = process_check()
    if running:
        raise AnalyseApplyRefused(
            "rekordbox_running", f"Quit Rekordbox fully first; still running: {', '.join(running)}"
        )
    if plan_sha != plan.plan_sha:
        raise AnalyseApplyRefused("plan_mismatch", "The confirmed plan is not the plan being applied; plan again")
    if plan.is_noop:
        raise AnalyseApplyRefused("nothing_to_do", "The plan has nothing to write")
    current = sha256_file(live_db)
    if current != plan.master_db_sha256:
        raise AnalyseApplyRefused("stale_plan", "Rekordbox's database changed since the plan was made; plan again")


# -----------------------------------------------------------------------------
# Backup and restore


def backup_rekordbox_dir(rb_dir: Path, backups_root: Path) -> Path:
    """Copy every top-level file of ``rb_dir`` (not ``share/``) into a new stamped folder."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = backups_root / stamp
    target.mkdir(parents=True)
    for item in sorted(rb_dir.iterdir()):
        if item.is_file():
            shutil.copy2(item, target / item.name)
    if not (target / "master.db").is_file():
        raise RuntimeError(f"backup of {rb_dir} holds no master.db")
    return target


def restore_rekordbox_dir(backup_dir: Path, rb_dir: Path) -> None:
    """Put every backed-up file back, and drop SQLite side files the write left behind."""
    for side in ("master.db-wal", "master.db-shm", "master.db-journal"):
        if not (backup_dir / side).exists():
            (rb_dir / side).unlink(missing_ok=True)
    for item in backup_dir.iterdir():
        shutil.copy2(item, rb_dir / item.name)


def prune_backups(backups_root: Path, keep: int = KEEP_BACKUPS) -> list[str]:
    stamps = sorted(p for p in backups_root.iterdir() if p.is_dir())
    pruned = stamps[:-keep] if len(stamps) > keep else []
    for path in pruned:
        shutil.rmtree(path)
    return [p.name for p in pruned]


# -----------------------------------------------------------------------------
# Write


def _open_writable(live_db: Path) -> Any:
    from pyrekordbox import Rekordbox6Database

    return Rekordbox6Database(path=str(live_db), unlock=not is_plain_sqlite(live_db))


def _close_fully(db: Any) -> None:
    """Fold the WAL into master.db and drop every pooled connection.

    Rekordbox keeps master.db in WAL mode. pyrekordbox's ``close()`` ends the
    session but leaves the engine's pooled connection open: a read-back that
    copies only master.db then misses the commit, and a restore that deletes
    the WAL file is overwritten when that connection checkpoints at exit.
    Roll back an uncommitted write before checkpointing so SQLite is not asked
    to checkpoint inside an outstanding write transaction, and close the
    session even when the checkpoint raises.
    """
    from sqlalchemy import text

    try:
        session = getattr(db, "session", None)
        if session is not None:
            with suppress(Exception):
                session.rollback()
            try:
                session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            finally:
                db.close()
    finally:
        db.engine.dispose()


def _playlist(db: Any, name: str) -> Any | None:
    matches = [pl for pl in db.get_playlist() if pl.Name == name]
    return matches[0] if matches else None


def _write(db: Any, plan: AnalysePlan) -> None:
    analyse_me = _playlist(db, ANALYSE_ME_PLAYLIST) or db.create_playlist(ANALYSE_ME_PLAYLIST)
    added = {path: db.add_content(path) for path in plan.add_paths}
    for content in added.values():
        db.add_to_playlist(analyse_me, content)
    for content_id in plan.playlist_add_ids:
        db.add_to_playlist(analyse_me, db.get_content(ID=content_id))
    if plan.playlist_remove_ids:
        drop = set(plan.playlist_remove_ids)
        for song in list(analyse_me.Songs):
            if str(song.ContentID) in drop:
                db.remove_from_playlist(analyse_me, song)
    if plan.viva_add_paths or plan.viva_add_ids:
        viva = _playlist(db, VIVA_TARGET_PLAYLIST)
        if viva is None:
            raise RuntimeError(f"{VIVA_TARGET_PLAYLIST!r} vanished between plan and write")
        for path in plan.viva_add_paths:
            db.add_to_playlist(viva, added[path])
        for content_id in plan.viva_add_ids:
            db.add_to_playlist(viva, db.get_content(ID=content_id))
    db.commit()


# -----------------------------------------------------------------------------
# Verify


def _read_back(live_db: Path) -> tuple[RbSnapshot, str]:
    with tempfile.TemporaryDirectory(prefix="rbx-analyse-verify-") as tmp:
        copy = copy_with_sidecars(live_db, Path(tmp))
        plain = copy if is_plain_sqlite(copy) else decrypt_to_plain(copy, Path(tmp) / "master.plain.db")
        return read_plain_rekordbox(plain, sha256_file(live_db)), integrity_ok(plain)


def _verify(plan: AnalysePlan, before: RbSnapshot, after: RbSnapshot, integrity: str) -> None:
    problems: list[str] = []
    if integrity != "ok":
        problems.append(f"integrity_check: {integrity}")
    before_paths = {normalise_path(t.folder_path) for t in before.tracks}
    after_by_path = {normalise_path(t.folder_path): t.content_id for t in after.tracks}
    expected_paths = before_paths | set(plan.add_paths)
    if set(after_by_path) != expected_paths:
        missing = sorted(expected_paths - set(after_by_path))[:5]
        extra = sorted(set(after_by_path) - expected_paths)[:5]
        problems.append(f"collection differs from plan: missing {missing}, unexpected {extra}")
    after_by_name = playlists_by_name(after.playlists)
    analyse_me = after_by_name.get(ANALYSE_ME_PLAYLIST)
    if analyse_me is None:
        problems.append(f"{ANALYSE_ME_PLAYLIST} missing after write")
    else:
        before_pl = playlists_by_name(before.playlists).get(ANALYSE_ME_PLAYLIST)
        expected = set(before_pl.member_ids) if before_pl else set()
        expected |= {after_by_path[p] for p in plan.add_paths if p in after_by_path}
        expected |= set(plan.playlist_add_ids)
        expected -= set(plan.playlist_remove_ids)
        if set(analyse_me.member_ids) != expected:
            problems.append(f"{ANALYSE_ME_PLAYLIST} members differ from plan")
    if plan.viva_add_paths or plan.viva_add_ids:
        viva = after_by_name.get(VIVA_TARGET_PLAYLIST)
        wanted = {after_by_path[p] for p in plan.viva_add_paths if p in after_by_path} | set(plan.viva_add_ids)
        if viva is None or not wanted <= set(viva.member_ids):
            problems.append(f"{VIVA_TARGET_PLAYLIST} is missing planned members")
    if problems:
        raise RuntimeError("; ".join(problems))


# -----------------------------------------------------------------------------
# Entry point


def apply_plan(
    plan: AnalysePlan,
    *,
    plan_sha: str,
    live_db: Path,
    backups_root: Path,
    before: RbSnapshot,
    cloud_sync_off_confirmed: bool,
    process_check: Callable[[], list[str]] | None = None,
) -> ApplyReport:
    """Write ``plan`` into Rekordbox behind every guard; restore the backup on any failure."""
    with _exclusive_target_lock(live_db):
        _check_guards(
            plan,
            live_db=live_db,
            plan_sha=plan_sha,
            cloud_sync_off_confirmed=cloud_sync_off_confirmed,
            process_check=process_check or running_rekordbox_processes,
        )
        if before.master_db_sha256 != plan.master_db_sha256:
            raise AnalyseApplyRefused("stale_plan", "The snapshot does not match the plan; plan again")
        backup_dir = backup_rekordbox_dir(live_db.parent, backups_root)
        try:
            db = _open_writable(live_db)
            try:
                _write(db, plan)
            finally:
                _close_fully(db)
            after, integrity = _read_back(live_db)
            _verify(plan, before, after, integrity)
        except Exception as error:
            try:
                restore_rekordbox_dir(backup_dir, live_db.parent)
            except Exception as restore_error:  # noqa: BLE001 - reported with the backup path
                raise AnalyseApplyFailed(
                    f"write failed ({error}) AND restore failed ({restore_error}); restore by hand from {backup_dir}",
                    backup_dir=backup_dir,
                    restored=False,
                ) from error
            raise AnalyseApplyFailed(
                f"write failed and Rekordbox was restored from the backup: {error}",
                backup_dir=backup_dir,
                restored=True,
            ) from error
        return ApplyReport(
            backup_dir=str(backup_dir),
            added=len(plan.add_paths),
            playlist_added=len(plan.add_paths) + len(plan.playlist_add_ids),
            playlist_removed=len(plan.playlist_remove_ids),
            viva_added=len(plan.viva_add_paths) + len(plan.viva_add_ids),
            master_db_sha256_after=after.master_db_sha256,
            integrity=integrity,
            pruned_backups=prune_backups(backups_root),
        )


__all__ = [
    "KEEP_BACKUPS",
    "REKORDBOX_PROCESSES",
    "SURFACE_ID",
    "AnalyseApplyFailed",
    "AnalyseApplyRefused",
    "ApplyReport",
    "apply_plan",
    "backup_rekordbox_dir",
    "prune_backups",
    "restore_rekordbox_dir",
    "running_rekordbox_processes",
]
