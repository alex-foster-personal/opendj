"""Deck-state pollers for the recorder.

Plan 12-01 Step 4 shipped two PULL sources that watch another app:

* ``djay_source`` -- wraps :mod:`apps.sync.djay_monitor`, reads the
  working copy of djay's MediaLibrary.db at 500 ms.
* ``rb_source``   -- tails the latest Rekordbox HISTORY playlist via
  direct SQL over a working copy of master.db.

``opendj_source`` covers our OWN decks and is PUSH-fed, because Open DJ's
decks are a browser Web Audio graph with no database to poll. The HTTP
ingest enqueues deck-state snapshots and the recorder's existing poll
thread drains them. See that module's docstring for why a track counts
as played only after a dwell threshold, which is a deliberate divergence
from the two sources above.

All three share the same interface: poll once (or until-stop in a
thread), emit :class:`apps.sets.state.Event` rows via a callback, and
keep errors out of the poll thread so the recorder keeps the other
sources going if one goes wrong.
"""
from __future__ import annotations

from .djay_source import DjaySource
from .opendj_source import DeckObservationError, OpenDjDeckSource
from .rb_source import RekordboxHistorySource

__all__ = [
    "DeckObservationError",
    "DjaySource",
    "OpenDjDeckSource",
    "RekordboxHistorySource",
]
