"""Shared state layer.

Canonical, local projection of the user's music library. Stores open-dj
stable_ids (OPEN-01b) and provenance-wrapped analysis fields (OPEN-01c) in
a single SQLite file (``data/state/state.db``). This package is the first
reference implementation of the open-dj v0 strawman in this repo.

Modules:
  paths      -- STATE_DB constant (re-exports from apps.shared.paths).
  schema     -- DDL + migrations.
  db         -- open_rw / open_ro context helpers + PRAGMA set.
  ids        -- stable_id algorithm (open-dj strawman v0, section 5).
  types      -- ProvenanceValue dataclass + Event dataclass + Source literal.
  provenance -- write_field / read_field / read_history / to_open_dj_track.
  events     -- in-process EventBus with durable log (append to ``events``).
  writer     -- StateWriter single write surface.
  ingest.rekordbox -- first adapter: RB master.db -> state.db.
  cli        -- argparse entry (init / stats / ingest-rb / inspect).
"""
from __future__ import annotations

__all__ = [
    "paths",
    "schema",
    "db",
    "ids",
    "types",
    "provenance",
    "events",
    "writer",
]
