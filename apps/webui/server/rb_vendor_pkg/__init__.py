"""T3b split target for ``apps.webui.server.rb_vendor`` (the monolith).

Each module here is a verbatim move of one cluster from the original
``rb_vendor.py`` (see ``.planning/t3b-decomposition-map.md`` section 2).
``rb_vendor.py`` re-exports every name back out of this package so its 10
route importers and test modules never change until the facade is retired
(T3b wave 4 / S8).

Modules:
  track_rows -- bulk row hydration for the browser listing read model
                (``build_track_rows`` and its ``bulk_*`` helpers).
  db -- read-only master.plain.db access (C4: playlist order + cue reads).
  anlz_cache -- C7 anlz JSON cache.
  beatgrid_issue_cache -- C8 beatgrid-issue sidecar cache.
  anlz -- PMAI waveform decode, PVDI vocals, ANLZ tag decode, payload
          assembly (C2/C3/C6/C9).

This package is a staging location: the map's final homes (S8 relocates)
are ``store/caches/`` for the caches and ``adapters/rekordbox/`` for db,
anlz, cues, reversal and writer.

S8 has moved the hot-cue cluster (C10: cues, reversal, writer) on to
``apps/adapters/rekordbox/``. That relocation is what closed the
``engine_core <-> webui`` package cycle: :mod:`apps.adapters.rekordbox.reversal`
is the module that imports the one sidecar-DDL home in
``apps.engine_core.store.schema``. The modules still listed above reach back
into ``rb_vendor`` for the C0/C1 constants and path helpers that slice S0
never extracted, so they cannot follow until that extraction lands.
"""
from __future__ import annotations
