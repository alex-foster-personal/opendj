"""Deck-state pollers for the recorder.

Plan 12-01 Step 4. Two v1 sources:

* ``djay_source`` -- wraps :mod:`apps.sync.djay_monitor`, reads the
  working copy of djay's MediaLibrary.db at 500 ms.
* ``rb_source``   -- tails the latest Rekordbox HISTORY playlist via
  direct SQL over a working copy of master.db.

Both sources share the :class:`SourceEmitter` interface: poll once
(or until-stop in a thread), emit :class:`apps.sets.state.Event`
rows via a callback, and swallow errors to a ``source_error`` event
so the recorder keeps the other source going if one goes wrong.
"""
from __future__ import annotations

from .djay_source import DjaySource
from .rb_source import RekordboxHistorySource

__all__ = ["DjaySource", "RekordboxHistorySource"]
