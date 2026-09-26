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

One named exception, for operational entry points only: ``hub_backup``
imports ``apps.cloud`` (the existing R2 client) and ``apps.engine_core``'s
stdlib-only ``config`` and ``lock`` (the data dir lock a restore must hold).
They are CLIs a host runs, never imported by the protocol modules above, so
the protocol itself stays bounded. ``hub_deploy`` imports only the stdlib and
``apps.shared``. The bind guard lives in ``apps.shared.sync_bind_guard`` so the
engine can enforce it without importing this package (no package cycle).
"""

from __future__ import annotations

__all__ = ["client", "conflict_trash", "engine", "protocol", "service", "sync_set"]
