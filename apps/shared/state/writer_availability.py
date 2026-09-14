"""``StateWriter`` availability upserts with durable events."""
from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol

from apps.shared.state.availability_write import (
    AvailabilityReport,
    AvailabilityRow,
    upsert_availability_rows,
)
from apps.shared.state.events import EventBus, FakeEventBus
from apps.shared.state.types import Event


class _WriterHost(Protocol):
    bus: EventBus | FakeEventBus
    _conn: Any

    def _tx(self) -> AbstractContextManager[Any]: ...

    def _now_iso(self) -> str: ...

    def _append_event(
        self,
        *,
        kind: str,
        stable_id: str | None,
        payload: dict[str, Any],
        ts: str | None = None,
    ) -> Event: ...


class _AvailabilityWriterMixin:
    def upsert_availability(
        self: _WriterHost,
        rows: list[AvailabilityRow],
        *,
        now: str | None = None,
        allow_mass_missing: bool = False,
        apply_mass_missing_guard: bool = True,
    ) -> AvailabilityReport:
        """Upsert availability rows and append one event per changed track."""
        stamp = now or self._now_iso()
        with self._tx():
            write_report = upsert_availability_rows(
                self._conn,
                rows,
                now=stamp,
                allow_mass_missing=allow_mass_missing,
                apply_mass_missing_guard=apply_mass_missing_guard,
            )
            for stable_id in write_report.changed_stable_ids:
                row = next(r for r in rows if r.stable_id == stable_id)
                ev = self._append_event(
                    kind="track.availability.upsert",
                    stable_id=stable_id,
                    payload={
                        "state": row.state,
                        "checked_path": row.checked_path,
                    },
                    ts=stamp,
                )
                self.bus.publish(ev)
        return AvailabilityReport(
            counts=write_report.counts,
            changed=write_report.changed,
            unchanged=write_report.unchanged,
            total=write_report.total,
        )


__all__ = ["_AvailabilityWriterMixin"]
