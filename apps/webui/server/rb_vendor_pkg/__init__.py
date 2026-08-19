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
  cues -- pure hot-cue modelling and validation, owns no connection (C10).
  reversal -- hot-cue reversal-token lifecycle and slot generations (C10).
  writer -- the only read-write surface into master.plain.db (C10).

This package is a staging location: the map's final homes (S8 relocates)
are ``store/caches/`` for the caches and ``adapters/rekordbox/`` for db,
anlz, cues, reversal and writer.
"""
from __future__ import annotations
