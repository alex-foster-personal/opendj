"""Handshake tests for the entry-gated peak cache publication contract (#13135).

The production cache is unchanged; these exercises use
``apps.webui.server.peak_cache.PeakCacheCoordinator`` on a disposable directory
with threading.Events instead of time.sleep polling.
"""

from __future__ import annotations

import threading
from pathlib import Path

from apps.webui.server.peak_cache import PeakCacheCoordinator, PeakCachePhase

_STRIP = {"preview_b64": "AA==", "marker": "strip"}
_ENTRY = {"peaks_b64": "AQ==", "marker": "entry"}


def _coordinator(tmp_path: Path) -> PeakCacheCoordinator:
    return PeakCacheCoordinator(root=tmp_path / "cache", stable_id="handshake-track")


def test_a_reader_reaching_the_cache_during_publish_sees_no_commit(tmp_path: Path) -> None:
    """[if] a reader reaches the cache during publish [then] entry gating reads absent, [else stop]."""
    coord = _coordinator(tmp_path)
    coord.reset_barriers()
    errors: list[str] = []

    def publish() -> None:
        try:
            coord.publish(strip=_STRIP, entry=_ENTRY)
        except (TimeoutError, OSError, ValueError) as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=publish, daemon=True)
    thread.start()
    assert coord.strip_ready.wait(timeout=5), "control: strip phase started"
    assert coord.phase() is PeakCachePhase.PUBLISHING
    assert coord.read_committed() is None
    coord.release_publish.set()
    assert coord.publish_complete.wait(timeout=5)
    thread.join(timeout=5)
    assert not errors
    assert coord.phase() is PeakCachePhase.PUBLISHED


def test_a_reader_after_publish_complete_sees_the_entry(tmp_path: Path) -> None:
    """[if] publish completes [then] read_committed returns the entry payload, [else stop]."""
    coord = _coordinator(tmp_path)
    coord.release_publish.set()
    coord.publish(strip=_STRIP, entry=_ENTRY)
    assert coord.phase() is PeakCachePhase.PUBLISHED
    assert coord.read_committed() == _ENTRY
    assert coord.strip_path().is_file()


def test_a_reader_when_the_cache_is_absent_sees_no_commit(tmp_path: Path) -> None:
    """[if] the cache is absent [then] phase is absent and read_committed is None, [else stop]."""
    coord = _coordinator(tmp_path)
    assert coord.phase() is PeakCachePhase.ABSENT
    assert coord.read_committed() is None
    assert not coord.strip_path().exists()
    assert not coord.entry_path().exists()
