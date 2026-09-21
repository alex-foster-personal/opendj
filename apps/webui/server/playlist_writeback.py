"""Fail-closed, CAS-protected vendor playlist writeback.

Plans name one immutable source membership snapshot, one *live* vendor database
path, one vendor playlist ID, and the target membership revision.  A live apply
must present that exact token and an explicit confirmation.  Names are display
data only - they are never used to select a vendor playlist.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from apps.shared import paths
from apps.shared.rekordbox_writeback import require_writeback_enabled
from apps.smartlists.diff import diff_sets

Vendor = Literal["rekordbox", "djay"]
TargetMode = Literal["live"]
VENDORS: tuple[Vendor, ...] = ("rekordbox", "djay")


@dataclass(frozen=True)
class VendorPlaylist:
    playlist_id: str
    name: str


@dataclass(frozen=True)
class WritebackBackup:
    backup_id: str


@dataclass(frozen=True)
class WritebackTarget:
    target_mode: TargetMode
    target_path: str
    target_id: str


class VendorPlaylistWriter(Protocol):
    vendor: str
    state_conn: sqlite3.Connection

    def list_playlists(self) -> list[VendorPlaylist]: ...
    def read_members_by_id(self, playlist_id: str) -> list[str]: ...
    def apply_with_backup_by_id(
        self, playlist_id: str, stable_members: list[str], expected_target_revision: str,
        expected_mapping_revision: str,
        source_transaction: Callable[[], AbstractContextManager[None]],
    ) -> tuple[WritebackBackup, str]: ...
    def restore_backup(self, backup_id: str, target_id: str, expected_target_revision: str) -> str: ...


WriterFactory = Callable[
    [Vendor, TargetMode, Path], AbstractContextManager[VendorPlaylistWriter | None]
]
SourceLockFactory = Callable[[], AbstractContextManager[None]]


def _canonical_live_path(vendor: Vendor) -> Path:
    if vendor == "rekordbox":
        return paths.REKORDBOX_LIVE_DB
    if vendor == "djay":
        return paths.DJAY_LIVE_DB
    raise ValueError(f"unknown writeback vendor: {vendor!r}")


def _require_exact_live_target(vendor: Vendor, target_mode: TargetMode, target_path: str) -> Path:
    if target_mode != "live":
        raise ValueError(f"{vendor}: target_mode must be the explicit literal 'live'")
    # Path.__eq__ is LEXICAL: it collapses neither `..` nor symlinks, so
    # '/live/../live/master.db' and a symlink pointing at master.db both
    # compare unequal to the canonical path while addressing the same file.
    # Resolve BOTH sides, and hand back the RESOLVED path so the handle that
    # gets opened is the one that was actually validated.
    expected = _canonical_live_path(vendor).resolve(strict=False)
    actual = Path(target_path).resolve(strict=False)
    if actual != expected:
        raise ValueError(
            f"{vendor}: refusing target {Path(target_path)} (resolves to {actual}); "
            f"exact live target is {expected}. "
            "Working copies are never writeback targets."
        )
    if not actual.exists():
        raise FileNotFoundError(f"{vendor}: live target does not exist: {actual}")
    return actual


@contextmanager
def default_writer_factory(
    vendor: Vendor, target_mode: TargetMode, target_path: Path, *,
    state_db_path: Path | None = None,
) -> Iterator[VendorPlaylistWriter | None]:
    """Yield a live-target writer and close every factory-owned resource."""
    _require_exact_live_target(vendor, target_mode, str(target_path))
    if state_db_path is None:
        raise WritebackUnavailable(
            "writeback mapping state database ownership is unavailable"
        )

    from apps.shared.state.db import open_ro

    with ExitStack() as resources:
        state_conn = resources.enter_context(closing(open_ro(state_db_path)))
        if vendor == "rekordbox":
            from apps.shared.rekordbox_db import open_db
            from apps.smartlists.rb_writer import RBPlaylistWriter

            rekordbox_db = resources.enter_context(closing(open_db(target_path)))
            writer: VendorPlaylistWriter | None = RBPlaylistWriter(
                db=rekordbox_db, state_conn=state_conn, live=True,
                live_db_path=target_path,
            )
        elif vendor == "djay":
            from apps.smartlists.djay_writer import build_djay_writer

            writer = build_djay_writer(target_path, state_conn=state_conn)
        else:
            raise ValueError(f"unknown writeback vendor: {vendor!r}")
        yield writer


def bind_default_writer_factory(state_db_path: str | Path) -> WriterFactory:
    """Bind production mapping reads to one backend-owned state database."""
    bound_path = Path(state_db_path)

    def factory(
        vendor: Vendor, target_mode: TargetMode, target_path: Path,
    ) -> AbstractContextManager[VendorPlaylistWriter | None]:
        return default_writer_factory(
            vendor, target_mode, target_path, state_db_path=bound_path,
        )

    return factory


@dataclass(frozen=True)
class WritebackCapability:
    vendor: str
    available: bool
    target_mode: TargetMode = "live"
    target_path: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class WritebackPlan:
    vendor: Vendor
    source_playlist_id: str
    target_mode: TargetMode
    target_path: str
    target_id: str
    target_name: str
    source_revision: str
    target_revision: str
    mapping_revision: str
    ordered_match: bool
    plan_token: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return self.ordered_match


@dataclass(frozen=True)
class WritebackApplyResult:
    vendor: Vendor
    target_id: str
    target_name: str
    applied: bool
    dry_run: bool
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    backup_id: str | None = None
    target_revision: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class WritebackRollbackResult:
    vendor: Vendor
    target_id: str
    backup_id: str
    rolled_back: bool
    target_revision: str


class WritebackUnavailable(RuntimeError):
    """A target cannot be opened safely."""


class WritebackConflict(RuntimeError):
    """A plan or rollback no longer matches current target state."""


def _revision(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class WritebackService:
    def __init__(
        self, *, writer_factory: WriterFactory = default_writer_factory,
        source_members_reader: Callable[[str], list[str]] | None = None,
        source_lock_factory: SourceLockFactory | None = None,
    ) -> None:
        self._writer_factory = writer_factory
        self._source_members_reader = source_members_reader
        self._source_lock_factory = source_lock_factory

    @contextmanager
    def _writer(
        self, vendor: Vendor, target_mode: TargetMode, target_path: str,
    ) -> Iterator[VendorPlaylistWriter]:
        with ExitStack() as writer_lifecycle:
            try:
                writer = writer_lifecycle.enter_context(
                    self._writer_factory(vendor, target_mode, Path(target_path))
                )
            except (FileNotFoundError, ValueError) as exc:
                raise WritebackUnavailable(str(exc)) from exc
            if writer is None:
                raise WritebackUnavailable(
                    f"{vendor}: exact live database is not reachable"
                )
            yield writer

    def capability(self, vendor: Vendor) -> WritebackCapability:
        path = _canonical_live_path(vendor)
        try:
            with self._writer(vendor, "live", str(path)):
                pass
        except WritebackUnavailable as exc:
            return WritebackCapability(vendor=vendor, available=False, target_path=str(path), reason=str(exc))
        return WritebackCapability(vendor=vendor, available=True, target_path=str(path))

    @staticmethod
    def _resolvable(writer: VendorPlaylistWriter, desired_ids: list[str]) -> tuple[list[str], list[str]]:
        if not desired_ids:
            return [], []
        unique = list(dict.fromkeys(desired_ids))
        placeholders = ",".join("?" * len(unique))
        rows = writer.state_conn.execute(
            "SELECT stable_id FROM track_vendor_ids WHERE vendor = ? "
            f"AND stable_id IN ({placeholders})", (writer.vendor, *unique),
        ).fetchall()
        known = {row[0] for row in rows}
        return [sid for sid in desired_ids if sid in known], [sid for sid in unique if sid not in known]

    @staticmethod
    def _mapping_revision(writer: VendorPlaylistWriter, desired_ids: list[str]) -> str:
        if not desired_ids:
            return _revision([])
        unique = list(dict.fromkeys(desired_ids))
        placeholders = ",".join("?" * len(unique))
        rows = writer.state_conn.execute(
            "SELECT stable_id, vendor_id FROM track_vendor_ids WHERE vendor = ? "
            f"AND stable_id IN ({placeholders})", (writer.vendor, *unique),
        ).fetchall()
        return _revision(sorted((str(stable_id), str(vendor_id)) for stable_id, vendor_id in rows))

    def _assert_source_snapshot(self, source_playlist_id: str, expected_source_revision: str) -> None:
        """Reject a source edit while its authoritative writer lock is held."""
        if self._source_members_reader is None:
            raise WritebackUnavailable("writeback source validator is unavailable")
        current = list(self._source_members_reader(source_playlist_id))
        actual = _revision({"source_playlist_id": source_playlist_id, "members": current})
        if actual != expected_source_revision:
            raise WritebackConflict("writeback source changed before vendor transaction")

    @contextmanager
    def _source_transaction(
        self, source_playlist_id: str, expected_source_revision: str,
    ) -> Iterator[None]:
        """Lock and validate the source before any vendor-side mutation.

        The state DB lock is acquired before a vendor target lock everywhere,
        which gives writeback one deadlock-safe lock ordering and prevents a
        source edit or vendor-ID remap between validation and the mutation.
        """
        if self._source_lock_factory is None:
            raise WritebackUnavailable("writeback source ownership lock is unavailable")
        with self._source_lock_factory():
            self._assert_source_snapshot(source_playlist_id, expected_source_revision)
            yield

    def targets(self, *, vendor: Vendor, target_mode: TargetMode, target_path: str) -> list[VendorPlaylist]:
        with self._writer(vendor, target_mode, target_path) as writer:
            return writer.list_playlists()

    def plan(
        self, *, vendor: Vendor, source_playlist_id: str, desired_ids: list[str],
        target_mode: TargetMode, target_path: str, target_id: str,
    ) -> WritebackPlan:
        with self._writer(vendor, target_mode, target_path) as writer:
            targets = {target.playlist_id: target for target in writer.list_playlists()}
            target = targets.get(target_id)
            if target is None:
                raise WritebackConflict(f"{vendor}: target playlist ID {target_id!r} does not exist")
            resolved, unresolved = self._resolvable(writer, desired_ids)
            current = writer.read_members_by_id(target_id)
            added, removed = diff_sets(current, resolved)
            source_revision = _revision({"source_playlist_id": source_playlist_id, "members": desired_ids})
            target_revision = _revision({"target_id": target_id, "members": current})
            mapping_revision = self._mapping_revision(writer, desired_ids)
            token_data = {
                "vendor": vendor, "source_playlist_id": source_playlist_id,
                "source_revision": source_revision, "target_mode": target_mode,
                "target_path": target_path, "target_id": target_id,
                "target_revision": target_revision, "added": added, "removed": removed,
                "unresolved": unresolved, "mapping_revision": mapping_revision,
            }
            return WritebackPlan(
                vendor=vendor, source_playlist_id=source_playlist_id, target_mode=target_mode,
                target_path=target_path, target_id=target_id, target_name=target.name,
                source_revision=source_revision, target_revision=target_revision, mapping_revision=mapping_revision, ordered_match=current == resolved,
                plan_token=_revision(token_data), added=added, removed=removed, unresolved=unresolved,
            )

    def apply(
        self,
        *,
        vendor: Vendor,
        source_playlist_id: str,
        desired_ids: list[str],
        target: WritebackTarget,
        plan_token: str,
        dry_run: bool = True,
        confirmed: bool = False,
    ) -> WritebackApplyResult:
        plan = self.plan(
            vendor=vendor, source_playlist_id=source_playlist_id, desired_ids=desired_ids,
            target_mode=target.target_mode, target_path=target.target_path, target_id=target.target_id,
        )
        if plan.plan_token != plan_token:
            raise WritebackConflict("writeback plan is stale: source or target revision changed; plan again")
        if plan.unresolved:
            return WritebackApplyResult(vendor, target.target_id, plan.target_name, False, dry_run,
                added=plan.added, removed=plan.removed,
                error=f"{len(plan.unresolved)} track(s) have no {vendor} mapping: {plan.unresolved[:5]}")
        if dry_run:
            return WritebackApplyResult(vendor, target.target_id, plan.target_name, False, True,
                added=plan.added, removed=plan.removed, target_revision=plan.target_revision)
        if not confirmed:
            raise WritebackConflict("live writeback requires confirmed=true")
        if vendor == "rekordbox":
            require_writeback_enabled("module.playlist_writeback.service_apply")
        with self._writer(vendor, target.target_mode, target.target_path) as writer:
            backup, target_revision = writer.apply_with_backup_by_id(
                target.target_id, desired_ids, plan.target_revision, plan.mapping_revision,
                lambda: self._source_transaction(source_playlist_id, plan.source_revision),
            )
        return WritebackApplyResult(vendor, target.target_id, plan.target_name, True, False,
            added=plan.added, removed=plan.removed, backup_id=backup.backup_id,
            target_revision=target_revision)

    def rollback(
        self, *, vendor: Vendor, target_mode: TargetMode, target_path: str,
        target_id: str, backup_id: str, expected_target_revision: str, confirmed: bool,
    ) -> WritebackRollbackResult:
        if not confirmed:
            raise WritebackConflict("rollback requires confirmed=true")
        # DELIBERATELY NOT GATED (mapped as gated=False): a recovery path only
        # exists after a gated apply already wrote, and gating it would trap
        # the user with a bad write and no undo. Every other rail still runs --
        # confirmed above, then pgrep, the exclusive target lock, the CAS on
        # current membership, and the manifest-provenance check inside
        # RBPlaylistWriter.restore_backup.
        with self._writer(vendor, target_mode, target_path) as writer:
            target_revision = writer.restore_backup(
                backup_id, target_id, expected_target_revision,
            )
        return WritebackRollbackResult(vendor, target_id, backup_id, True, target_revision)


__all__ = [
    "TargetMode", "VENDORS", "Vendor", "VendorPlaylist", "VendorPlaylistWriter",
    "WritebackApplyResult", "WritebackBackup", "WritebackCapability", "WritebackConflict",
    "WritebackPlan", "WritebackRollbackResult", "WritebackService", "WritebackUnavailable",
    "WriterFactory", "bind_default_writer_factory", "default_writer_factory",
]
