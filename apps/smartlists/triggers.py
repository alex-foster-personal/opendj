"""Re-evaluation triggers (SMART-03).

Subscribes to the Phase 5 state event bus (via an opaque ``event_bus``
with ``.subscribe / .unsubscribe``) and re-materialises only the
smartlists whose referenced fields overlap the event's
``changed_fields``. Per-smartlist 5s debounce coalesces bursts.

Phase 5 note: ``apps.shared.state.events`` shipped with ``EventBus``
and a ``types.Event`` dataclass (``ts``, ``kind``, ``stable_id``,
``payload``, ``actor``) whose ``subscribe(kind, callback)`` surface
does not carry ``changed_fields``. The local :class:`StateEvent` below
stays the smartlists-facing DTO; a thin adapter can project the Phase
5 ``Event.payload["changed_fields"]`` into this shape when the writer
starts emitting field-scoped events. Until then, runners pass either a
``FakeEventBus``-style bus in tests or a shim that constructs
:class:`StateEvent` values directly.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from apps.smartlists.debounce import Debouncer
from apps.smartlists.materializer import Materializer, MaterializeResult
from apps.smartlists.repo import SmartlistsRepo

_MEMBERSHIP_KINDS: frozenset[str] = frozenset({
    "track.added",
    "track.removed",
})


@dataclass(frozen=True)
class StateEvent:
    kind: str
    stable_id: str | None = None
    changed_fields: frozenset[str] = field(default_factory=frozenset)


class TriggerRunner:
    """Coordinates event-driven smartlist re-evaluation."""

    def __init__(
        self,
        sm_repo: SmartlistsRepo,
        materializer: Materializer,
        *,
        debounce_seconds: float = 5.0,
        field_scope: bool = True,
        dry_run: bool = False,
        live: bool = True,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.sm_repo = sm_repo
        self.materializer = materializer
        if clock is not None:
            self.debouncer = Debouncer(
                window_seconds=debounce_seconds, clock=clock,
            )
        else:
            self.debouncer = Debouncer(window_seconds=debounce_seconds)
        self.field_scope = field_scope
        self.dry_run = dry_run
        self.live = live

    def subscribe_to(self, event_bus) -> None:
        """Subscribe to a Phase 5 ``EventBus``-style bus.

        The real Phase 5 bus (``apps.shared.state.events.EventBus``)
        exposes ``subscribe(kind, callback)`` and emits ``types.Event``
        instances -- not :class:`StateEvent`. We register a single
        wildcard subscriber and adapt each ``Event`` into a
        :class:`StateEvent` before handing it to :meth:`handle_event`,
        projecting ``payload['changed_fields']`` into the smartlists
        DTO shape.
        """
        event_bus.subscribe("*", self._on_bus_event)

    def unsubscribe_from(self, event_bus) -> None:
        # EventBus has no unsubscribe; support fakes that do, otherwise no-op.
        unsub = getattr(event_bus, "unsubscribe", None)
        if unsub is not None:
            unsub("*", self._on_bus_event)

    def _on_bus_event(self, event) -> None:
        """Adapt a Phase 5 ``Event`` (or a ``StateEvent``) to our handler."""
        if isinstance(event, StateEvent):
            self.handle_event(event)
            return
        payload = getattr(event, "payload", None) or {}
        raw_fields = payload.get("changed_fields") or ()
        try:
            changed = frozenset(raw_fields)
        except TypeError:
            changed = frozenset()
        self.handle_event(StateEvent(
            kind=getattr(event, "kind", ""),
            stable_id=getattr(event, "stable_id", None),
            changed_fields=changed,
        ))

    def handle_event(self, event: StateEvent) -> None:
        always_wake = event.kind in _MEMBERSHIP_KINDS
        changed = frozenset(event.changed_fields or ())
        for row in self.sm_repo.list_all():
            if always_wake or not self.field_scope:
                self.debouncer.arm(row.id)
                continue
            if row.referenced_fields & changed:
                self.debouncer.arm(row.id)

    def arm_all(self) -> None:
        for row in self.sm_repo.list_all():
            self.debouncer.arm(row.id)

    def run_ready(
        self,
        *,
        now: float | None = None,
    ) -> list[MaterializeResult]:
        ready_ids = self.debouncer.ready(now=now)
        results: list[MaterializeResult] = []
        for sid in ready_ids:
            results.append(
                self.materializer.materialize(
                    sid, dry_run=self.dry_run, live=self.live,
                )
            )
        return results

    def pending(self) -> list[str]:
        return self.debouncer.pending()


__all__ = ["StateEvent", "TriggerRunner"]
