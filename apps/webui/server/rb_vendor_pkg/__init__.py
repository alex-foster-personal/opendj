"""T3b split target for ``apps.webui.server.rb_vendor`` (the monolith).

Each module here is a verbatim move of one cluster from the original
``rb_vendor.py`` (see ``.planning/t3b-decomposition-map.md`` section 2).
``rb_vendor.py`` re-exports every name back out of this package so its 10
route importers and test modules never change until the facade is retired
(T3b wave 4 / S8).

Modules:
  track_rows -- bulk row hydration for the browser listing read model
                (``build_track_rows`` and its ``bulk_*`` helpers).
"""
from __future__ import annotations
