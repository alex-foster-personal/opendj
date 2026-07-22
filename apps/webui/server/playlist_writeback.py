"""Fail-closed, CAS-protected vendor playlist writeback.

Plans name one immutable source membership snapshot, one *live* vendor database
path, one vendor playlist ID, and the target membership revision.  A live apply
must present that exact token and an explicit confirmation.  Names are display
data only - they are never used to select a vendor playlist.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional, Protocol

from apps.shared import paths
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


class VendorPlaylistWriter(Protocol):
    vendor: str
    state_conn: object

    def list_playlists(self) -> list[VendorPlaylist]: ...
    def read_members_by_id(self, playlist_id: str) -> list[str]: ...
    def apply_with_backup_by_id(
        self, playlist_id: str, native_members: list[str], stable_members: list[str], expected_target_revision: str, expected_mapping_revision: str,
    ) -> tuple[WritebackBackup, str]: ...
    def restore_backup(self, backup_id: str, target_id: str, expected_target_revision: str) -> str: ...


WriterFactory = Callable[[Vendor, TargetMode, Path], Optional[VendorPlaylistWriter]]


def _canonical_live_path(vendor: Vendor) -> Path:
    if vendor == "rekordbox":
        return paths.REKORDBOX_LIVE_DB
    if vendor == "djay":
        return paths.DJAY_LIVE_DB
    raise ValueError(f"unknown writeback vendor: {vendor!r}")


def _require_exact_live_target(vendor: Vendor, target_mode: TargetMode, target_path: str) -> Path:
    if target_mode != "live":
        raise ValueError(f"{vendor}: target_mode must be the explicit literal 'live'")
    expected = _canonical_live_path(vendor)
    actual = Path(target_path)
    if actual != expected:
        raise ValueError(
            f"{vendor}: refusing target {actual}; exact live target is {expected}. "
            "Working copies are never writeback targets."
        )
    if not actual.exists():
        raise FileNotFoundError(f"{vendor}: live target does not exist: {actual}")
    return actual


def default_writer_factory(
    vendor: Vendor, target_mode: TargetMode, target_path: Path,
) -> Optional[VendorPlaylistWriter]:
    """Open only the exact live target.  No production fallback exists."""
    _require_exact_live_target(vendor, target_mode, str(target_path))
    if vendor == "rekordbox":
        from apps.shared.rekordbox_db import open_db
        from apps.smartlists.rb_writer import RBPlaylistWriter
        from apps.shared.state.db import open_ro

        return RBPlaylistWriter(
            db=open_db(target_path), state_conn=open_ro(), live=True,
            live_db_path=target_path,
        )
    if vendor == "djay":
        from apps.smartlists.djay_writer import build_djay_writer

        return build_djay_writer(target_path)
    raise ValueError(f"unknown writeback vendor: {vendor!r}")


@dataclass(frozen=True)
class WritebackCapability:
    vendor: str
    available: bool
    target_mode: TargetMode = "live"
    target_path: Optional[str] = None
    reason: Optional[str] = None


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
    backup_id: Optional[str] = None
    target_revision: Optional[str] = None
    error: Optional[str] = None


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
    def __init__(self, *, writer_factory: WriterFactory = default_writer_factory) -> None:
        self._writer_factory = writer_factory

    def _writer(
        self, vendor: Vendor, target_mode: TargetMode, target_path: str,
    ) -> VendorPlaylistWriter:
        try:
            writer = self._writer_factory(vendor, target_mode, Path(target_path))
        except (FileNotFoundError, ValueError) as exc:
            raise WritebackUnavailable(str(exc)) from exc
        if writer is None:
            raise WritebackUnavailable(f"{vendor}: exact live database is not reachable")
        return writer

    def capability(self, vendor: Vendor) -> WritebackCapability:
        path = _canonical_live_path(vendor)
        try:
            self._writer(vendor, "live", str(path))
        except WritebackUnavailable as exc:
            return WritebackCapability(vendor=vendor, available=False, target_path=str(path), reason=str(exc))
        return WritebackCapability(vendor=vendor, available=True, target_path=str(path))

    @staticmethod
    def _resolvable(writer: VendorPlaylistWriter, desired_ids: list[str]) -> tuple[list[str], list[str]]:
        if not desired_ids:
            return [], []
        unique = list(dict.fromkeys(desired_ids))
        placeholders = ",".join("?" * len(unique))
        rows = writer.state_conn.execute(  # type: ignore[attr-defined]
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
        rows = writer.state_conn.execute(  # type: ignore[attr-defined]
            "SELECT stable_id, vendor_id FROM track_vendor_ids WHERE vendor = ? "
            f"AND stable_id IN ({placeholders})", (writer.vendor, *unique),
        ).fetchall()
        return _revision(sorted((str(stable_id), str(vendor_id)) for stable_id, vendor_id in rows))

    @staticmethod
    def _native_payload(writer: VendorPlaylistWriter, desired_ids: list[str]) -> list[str]:
        if not desired_ids:
            return []
        unique = list(dict.fromkeys(desired_ids))
        placeholders = ",".join("?" * len(unique))
        rows = writer.state_conn.execute(  # type: ignore[attr-defined]
            "SELECT stable_id, vendor_id FROM track_vendor_ids WHERE vendor = ? "
            f"AND stable_id IN ({placeholders})", (writer.vendor, *unique),
        ).fetchall()
        mapping = {str(stable_id): str(vendor_id) for stable_id, vendor_id in rows}
        missing = [stable_id for stable_id in desired_ids if stable_id not in mapping]
        if missing:
            raise WritebackConflict(f"{writer.vendor}: desired mapping changed before write: {missing[:5]}")
        return [mapping[stable_id] for stable_id in desired_ids]

    def targets(self, *, vendor: Vendor, target_mode: TargetMode, target_path: str) -> list[VendorPlaylist]:
        return self._writer(vendor, target_mode, target_path).list_playlists()

    def plan(
        self, *, vendor: Vendor, source_playlist_id: str, desired_ids: list[str],
        target_mode: TargetMode, target_path: str, target_id: str,
    ) -> WritebackPlan:
        writer = self._writer(vendor, target_mode, target_path)
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
        self, *, vendor: Vendor, source_playlist_id: str, desired_ids: list[str],
        target_mode: TargetMode, target_path: str, target_id: str, plan_token: str,
        dry_run: bool = True, confirmed: bool = False,
    ) -> WritebackApplyResult:
        plan = self.plan(
            vendor=vendor, source_playlist_id=source_playlist_id, desired_ids=desired_ids,
            target_mode=target_mode, target_path=target_path, target_id=target_id,
        )
        if plan.plan_token != plan_token:
            raise WritebackConflict("writeback plan is stale: source or target revision changed; plan again")
        if plan.unresolved:
            return WritebackApplyResult(vendor, target_id, plan.target_name, False, dry_run,
                added=plan.added, removed=plan.removed,
                error=f"{len(plan.unresolved)} track(s) have no {vendor} mapping: {plan.unresolved[:5]}")
        if dry_run:
            return WritebackApplyResult(vendor, target_id, plan.target_name, False, True,
                added=plan.added, removed=plan.removed, target_revision=plan.target_revision)
        if not confirmed:
            raise WritebackConflict("live writeback requires confirmed=true")
        writer = self._writer(vendor, target_mode, target_path)
        native_payload = self._native_payload(writer, desired_ids)
        if self._mapping_revision(writer, desired_ids) != plan.mapping_revision:
            raise WritebackConflict("writeback mapping changed before immutable vendor payload was captured")
        backup, target_revision = writer.apply_with_backup_by_id(
            target_id, native_payload, desired_ids, plan.target_revision, plan.mapping_revision,
        )
        return WritebackApplyResult(vendor, target_id, plan.target_name, True, False,
            added=plan.added, removed=plan.removed, backup_id=backup.backup_id,
            target_revision=target_revision)

    def rollback(
        self, *, vendor: Vendor, target_mode: TargetMode, target_path: str,
        target_id: str, backup_id: str, expected_target_revision: str, confirmed: bool,
    ) -> WritebackRollbackResult:
        if not confirmed:
            raise WritebackConflict("rollback requires confirmed=true")
        writer = self._writer(vendor, target_mode, target_path)
        target_revision = writer.restore_backup(backup_id, target_id, expected_target_revision)
        return WritebackRollbackResult(vendor, target_id, backup_id, True, target_revision)


__all__ = [
    "TargetMode", "VENDORS", "Vendor", "VendorPlaylist", "VendorPlaylistWriter",
    "WritebackApplyResult", "WritebackBackup", "WritebackCapability", "WritebackConflict",
    "WritebackPlan", "WritebackRollbackResult", "WritebackService", "WritebackUnavailable",
    "WriterFactory", "default_writer_factory",
]
