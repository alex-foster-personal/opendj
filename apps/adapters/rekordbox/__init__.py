"""Rekordbox adapter -- the vendor package ``apps/adapters/__init__`` reserved.

T3b wave 4 (``.planning/t3b-decomposition-map.md`` section 2) gives the
hot-cue write surface its final home here, out of ``apps/webui``. The three
modules are verbatim moves of the map's targets 10-12:

  cues -- pure hot-cue modelling and validation, owns no connection.
  reversal -- reversal-token lifecycle and slot generations.
  writer -- the only read-write surface into rekordbox's master.plain.db.

Leaving ``apps.webui`` is what closes the ``engine_core <-> webui`` package
cycle recorded in ``ops/quality/baseline.json``: :mod:`.reversal` provisions
the hot-cue sidecar through the one DDL home in
``apps.engine_core.store.schema`` (map defect D2), while the engine chassis
imports ``apps.webui`` to compose the legacy app. With the importer sitting in
``apps.adapters`` the edge no longer closes a loop.

Connections are injected, never resolved here: every entry point takes a
zero-argument factory, so this package carries no path constant and no
opinion about which database file it reads or writes.

Known carry-over: these modules still raise ``fastapi.HTTPException``. The
map's target #3 (``adapters/rekordbox/errors.py``, a domain error type with
one mapping table in the HTTP layer) belongs to slice S0 and was never built,
so the decomposition's "no fastapi under adapters/" clause is not yet met.
Swapping the type here would change every asserted status code in
``tests/webui/test_rb_hot_cue_write.py``, so it stays a named debt rather than
a drive-by rewrite.
"""
from __future__ import annotations
