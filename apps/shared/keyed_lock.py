"""In-process registry: one ``threading.Lock`` per hashable key, created on first use.

Supersedes: the ``_track_lock`` body in ``apps/analysis_waveform/local_waveform.py``,
the ``_cache_lock`` bodies in ``apps/webui/server/rb_vendor_pkg/anlz_cache.py`` and
``apps/webui/server/rb_vendor_pkg/beatgrid_issue_cache.py``, and ``_path_lock`` in
``apps/webui/server/coverage_outcomes.py``.
"""

from __future__ import annotations

import threading
from collections.abc import Hashable


class KeyedLocks:
    """One lock per key for the lifetime of this instance; keys are never evicted."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[Hashable, threading.Lock] = {}

    def lock_for(self, key: Hashable) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock
