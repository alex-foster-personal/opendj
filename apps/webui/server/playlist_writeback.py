"""Playlist writeback service -- push a webui-canonical playlist's
membership into rekordbox / djay.

CONTRACT -- playlists-router lane, node ``write-back-rekordbox-djay``
=======================================================================
Reuses the smartlists materialiser rails AS-IS:
:func:`apps.smartlists.diff.diff_sets` for the add/remove computation and
:class:`apps.smartlists.rb_writer.RBPlaylistWriter` /
:class:`apps.smartlists.djay_writer.DjayPlaylistWriter` for the vendor-side
mutation. ``live=True`` on the RB writer enables its own pgrep + one-shot
backup safety rails -- the exact same construction
:func:`apps.smartlists.refresh._build_writers` uses for smartlist
materialisation; djay relies on its own per-op transactional self-guard.
No new safety mechanism is invented here.

Unlike a smartlist (whose "old" membership is a persisted
``last_materialized_track_ids`` column), a webui playlist keeps no
per-vendor writeback record: the "old" side of the diff is read straight
off the LIVE target playlist via the ``read_members`` hook this feature
adds to both writer classes. A re-run always diffs against current
adapter state, so writeback is naturally idempotent (no cache to go
stale, nothing to reconcile on a retry after a partial failure).

Vendor DB absence is a normal, explicit outcome, never a silent skip:
:func:`default_writer_factory` returns ``None`` (via
``build_rb_writer``/``build_djay_writer``) when the vendor database isn't
reachable, and :meth:`WritebackService.capability` surfaces that as
``available=False`` with a reason string -- the router turns an
unavailable vendor into an explicit 503, never a fabricated empty plan.

Target playlist naming: the vendor-side playlist takes the SAME name as
the webui playlist (no marker prefix -- unlike smartlist materialisation,
this is a direct user-authored 1:1 correspondence). Because a live target
with that name might predate this feature and belong to something else
entirely, ``apply()`` refuses to mutate an already-existing target unless
the caller passes ``force_adopt=True`` -- the API/UI's plan-then-apply
flow is the safety gate (the diff is always shown before that flag can be
set), so no additional persisted "did we make this" bookkeeping is kept.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Optional, Protocol

from apps.smartlists.diff import diff_sets

Vendor = Literal["rekordbox", "djay"]
VENDORS: tuple[Vendor, ...] = ("rekordbox", "djay")


class VendorPlaylistWriter(Protocol):
    """Narrow surface this service needs from a vendor writer."""

    vendor: str
    state_conn: object  # sqlite3.Connection; typed loosely to avoid import

    def playlist_exists(self, name: str) -> bool: ...
    def read_members(self, name: str) -> list[str]: ...
    def create_playlist(self, name: str, track_ids: list[str]) -> None: ...
    def apply_diff(
        self, name: str, added: list[str], removed: list[str],
    ) -> None: ...


WriterFactory = Callable[[str], Optional[VendorPlaylistWriter]]


def default_writer_factory(vendor: str) -> Optional[VendorPlaylistWriter]:
    """Production factory: real RB/djay writers, ``None`` when unreachable.

    Mirrors ``apps.smartlists.refresh._build_writers(live=True)`` -- the
    RB writer is constructed live (pgrep + backup rails armed) since
    writeback always performs a real mutation when it isn't a dry run;
    the djay writer carries its own per-op self-guard regardless.
    """
    if vendor == "rekordbox":
        from apps.smartlists.rb_writer import build_rb_writer

        return build_rb_writer(live=True)
    if vendor == "djay":
        from apps.smartlists.djay_writer import build_djay_writer

        return build_djay_writer()
    raise ValueError(f"unknown writeback vendor: {vendor!r}")


@dataclass(frozen=True)
class WritebackCapability:
    vendor: str
    available: bool
    reason: Optional[str] = None


@dataclass(frozen=True)
class WritebackPlan:
    vendor: str
    playlist_name: str
    target_exists: bool
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not self.added and not self.removed


@dataclass(frozen=True)
class WritebackApplyResult:
    vendor: str
    playlist_name: str
    applied: bool
    dry_run: bool
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


class WritebackUnavailable(RuntimeError):
    """Raised when the requested vendor's database isn't reachable."""


class WritebackService:
    """Plan/apply playlist writeback for one vendor at a time.

    Stateless across calls (see module docstring): every ``plan``/
    ``apply`` re-derives the diff from the live target, so there is
    nothing to construct with beyond an optional test-injected
    ``writer_factory``.
    """

    def __init__(self, *, writer_factory: WriterFactory = default_writer_factory) -> None:
        self._writer_factory = writer_factory

    def _writer(self, vendor: str) -> VendorPlaylistWriter:
        writer = self._writer_factory(vendor)
        if writer is None:
            raise WritebackUnavailable(
                f"{vendor}: database not reachable; nothing to write to"
            )
        return writer

    def capability(self, vendor: str) -> WritebackCapability:
        try:
            self._writer(vendor)
        except WritebackUnavailable as exc:
            return WritebackCapability(vendor=vendor, available=False, reason=str(exc))
        return WritebackCapability(vendor=vendor, available=True)

    @staticmethod
    def _resolvable(
        writer: VendorPlaylistWriter, desired_ids: list[str],
    ) -> tuple[list[str], list[str]]:
        """Split ``desired_ids`` into (resolvable, unresolved) via the
        state layer's ``track_vendor_ids`` map, preserving order."""
        if not desired_ids:
            return [], []
        unique = list(dict.fromkeys(desired_ids))
        placeholders = ",".join("?" * len(unique))
        rows = writer.state_conn.execute(  # type: ignore[attr-defined]
            "SELECT stable_id FROM track_vendor_ids "
            f"WHERE vendor = ? AND stable_id IN ({placeholders})",
            (writer.vendor, *unique),
        ).fetchall()
        known = {r[0] for r in rows}
        resolved = [sid for sid in desired_ids if sid in known]
        unresolved = [sid for sid in unique if sid not in known]
        return resolved, unresolved

    def plan(
        self, *, vendor: str, playlist_name: str, desired_ids: list[str],
    ) -> WritebackPlan:
        writer = self._writer(vendor)
        resolved, unresolved = self._resolvable(writer, desired_ids)
        exists = writer.playlist_exists(playlist_name)
        current = writer.read_members(playlist_name) if exists else []
        added, removed = diff_sets(current, resolved)
        return WritebackPlan(
            vendor=vendor, playlist_name=playlist_name, target_exists=exists,
            added=added, removed=removed, unresolved=unresolved,
        )

    def apply(
        self,
        *,
        vendor: str,
        playlist_name: str,
        desired_ids: list[str],
        dry_run: bool = True,
        force_adopt: bool = False,
    ) -> WritebackApplyResult:
        writer = self._writer(vendor)
        resolved, unresolved = self._resolvable(writer, desired_ids)
        if unresolved:
            return WritebackApplyResult(
                vendor=vendor, playlist_name=playlist_name, applied=False,
                dry_run=dry_run,
                error=(
                    f"{len(unresolved)} track(s) have no {vendor} mapping "
                    f"(first: {unresolved[:5]}); resolve before writing back"
                ),
            )
        exists = writer.playlist_exists(playlist_name)
        current = writer.read_members(playlist_name) if exists else []
        added, removed = diff_sets(current, resolved)

        if dry_run:
            return WritebackApplyResult(
                vendor=vendor, playlist_name=playlist_name, applied=False,
                dry_run=True, added=added, removed=removed,
            )
        if exists and not force_adopt:
            return WritebackApplyResult(
                vendor=vendor, playlist_name=playlist_name, applied=False,
                dry_run=False, added=added, removed=removed,
                error=(
                    f"{vendor}: playlist {playlist_name!r} already exists; "
                    "review the plan and retry with force_adopt=true"
                ),
            )
        try:
            if exists:
                writer.apply_diff(playlist_name, added, removed)
            else:
                writer.create_playlist(playlist_name, resolved)
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            return WritebackApplyResult(
                vendor=vendor, playlist_name=playlist_name, applied=False,
                dry_run=False, added=added, removed=removed, error=str(exc),
            )
        return WritebackApplyResult(
            vendor=vendor, playlist_name=playlist_name, applied=True,
            dry_run=False, added=added, removed=removed,
        )


__all__ = [
    "VENDORS",
    "Vendor",
    "VendorPlaylistWriter",
    "WritebackApplyResult",
    "WritebackCapability",
    "WritebackPlan",
    "WritebackService",
    "WritebackUnavailable",
    "WriterFactory",
    "default_writer_factory",
]
