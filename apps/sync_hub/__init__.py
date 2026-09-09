"""Machine-to-machine library sync: protocol, engine, hub service, spoke client.

Contract: ``specs/design_decision_04.md`` (protocol, conflict rule, sync set)
over the schema in ``specs/design_decision_05.md`` (migration v6).

Modules:
  protocol -- wire dataclasses, canonical row serialization, per-table digest.
  sync_set -- which STORED rows are in the sync set and which are held back.
              ONE definition, read by the offer and the digest, because both
              must exclude the same rows.
  engine   -- push selection, last-writer-wins apply, changelog, watermarks.
  service  -- FastAPI router the hub serves at ``/api/v1/sync/*``.
  client   -- spoke-side ``run_sync``: hello, push, pull, digest compare.

Bounded on purpose: this package imports ``apps.shared`` and nothing else
from ``apps``. The webui includes :data:`apps.sync_hub.service.router`; the
dependency never points the other way.
"""
from __future__ import annotations

__all__ = ["client", "engine", "protocol", "service", "sync_set"]
