"""Bidirectional hub-and-spoke sync for the shared state DB (CLOUDSYNC).

Contract: ``specs/design_decision_04.md`` (protocol, conflict rule, sync set)
over the schema in ``specs/design_decision_05.md`` (migration v6).

Modules:
  protocol -- wire dataclasses, canonical row serialization, per-table digest.
  engine   -- push selection, last-writer-wins apply, changelog, watermarks.
  service  -- FastAPI router the hub serves at ``/api/v1/sync/*``.
  client   -- spoke-side ``run_sync``: hello, push, pull, digest compare.

Bounded on purpose: this package imports ``apps.shared`` and nothing else
from ``apps``. The webui includes :data:`apps.sync_hub.service.router`; the
dependency never points the other way.
"""
from __future__ import annotations

__all__ = ["client", "engine", "protocol", "service"]
