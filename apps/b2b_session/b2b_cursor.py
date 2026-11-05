"""B2BCURSOR-01..B2BCURSOR-03: the other DJ's mouse cursor, pure half (no FastAPI, no I/O).

Each engine keeps ONE slot: its own page's latest cursor position, overwritten on
every send, never queued, never persisted. The other engine pulls that slot with
``GET /api/v1/b2b/cursor/latest`` (same peer URL + token as the PEER row and the
shout outbox), so the LAN door (``peer_listener``) stays GET-only.

A position is NOT raw pixels: it is ``(anchor, fx, fy)``, an anchor id naming a
shared layout region (``deck-1`` .. ``deck-4``, ``mixer``, ``browser``, ``topbar``,
or ``viewport``) plus the pointer's fraction across that region's box. Two screens of
different sizes and layouts then agree on "the middle of their deck 1's waveform".
``(vx, vy)`` is the same pointer as a fraction of the sender's viewport: the fallback
when the anchor (after the deck remap) is not on the reader's screen.
The deck remap (their deck 1 is my deck 3) is a view decision and lives in the page
(``src/lib/rb/b2b-cursor.ts``), not here.

``age_ms`` is measured on the SENDER's monotonic clock at read time, so the reader
never compares two machines' wall clocks.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, TypeGuard

from apps.shared.link_constants import LINK

#: Anchor ids a page may send (LINKCONST-01: the page's CURSOR_ANCHORS, one JSON source).
CURSOR_ANCHORS: tuple[str, ...] = LINK.cursor_anchors
#: Server-side ceiling (25 Hz). The page throttles itself to SEND_INTERVAL_MS (66 ms);
#: this only stops a runaway client from burning the peer's poll budget.
MIN_SEND_INTERVAL_MS = 40
#: Every cursor carries these four fractions: (fx, fy) in the anchor box, (vx, vy) in the viewport.
FRACTION_KEYS: tuple[str, ...] = ("fx", "fy", "vx", "vy")


class CursorRateLimited(Exception):
    """A send arrived sooner than MIN_SEND_INTERVAL_MS after the previous one."""

    def __init__(self, retry_after_ms: int) -> None:
        super().__init__(f"cursor sends are capped at one per {MIN_SEND_INTERVAL_MS} ms; retry in {retry_after_ms} ms")
        self.retry_after_ms = retry_after_ms


def validate_position(anchor: str, fractions: dict[str, float]) -> None:
    """Raise ValueError unless ``anchor`` is known and every fraction is finite and in [0, 1]."""
    if anchor not in CURSOR_ANCHORS:
        raise ValueError(f"unknown cursor anchor {anchor!r}; known: {', '.join(CURSOR_ANCHORS)}")
    for name, value in fractions.items():
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be a fraction in [0, 1], got {value!r}")


@dataclass
class CursorSlot:
    """This engine's own page cursor: the latest position only."""

    machine: str
    epoch: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    _seq: int = 0
    _latest: dict[str, Any] | None = None
    _at_mono: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def post(self, anchor: str, fractions: dict[str, float], visible: bool) -> dict[str, Any]:
        """Overwrite the slot with ``fractions`` = ``{fx, fy, vx, vy}``.

        ``visible=False`` is the page saying the pointer left the window.
        """
        if set(fractions) != set(FRACTION_KEYS):
            raise ValueError(f"a cursor needs exactly {', '.join(FRACTION_KEYS)}, got {', '.join(sorted(fractions))}")
        validate_position(anchor, fractions)
        now = time.monotonic()
        with self._lock:
            since_ms = (now - self._at_mono) * 1000.0
            if self._seq > 0 and since_ms < MIN_SEND_INTERVAL_MS:
                raise CursorRateLimited(math.ceil(MIN_SEND_INTERVAL_MS - since_ms))
            self._seq += 1
            self._at_mono = now
            self._latest = {"anchor": anchor, **fractions, "visible": visible, "seq": self._seq}
            return {"seq": self._seq, "epoch": self.epoch}

    def latest(self) -> dict[str, Any]:
        """``{machine, epoch, seq, cursor}``; ``cursor`` carries ``age_ms`` on this machine's clock, or is None."""
        with self._lock:
            cursor = None
            if self._latest is not None:
                cursor = {**self._latest, "age_ms": round((time.monotonic() - self._at_mono) * 1000.0)}
            return {"machine": self.machine, "epoch": self.epoch, "seq": self._seq, "cursor": cursor}


def _is_seq(value: Any) -> bool:
    """A sequence number: a non-negative int (bool is an int subclass, so it is excluded)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _check_peer_cursor(cursor: Any, page_seq: int) -> None:
    """Raise TypeError/ValueError unless ``cursor`` is a complete cursor stamped with ``page_seq``."""
    if not isinstance(cursor, dict) or not _is_seq(cursor.get("age_ms")) or not isinstance(cursor.get("visible"), bool):
        raise TypeError("peer cursor has no non-negative age_ms / boolean visible")
    # The page keys a cursor by epoch:seq; the slot stamps the cursor with the page's seq.
    seq = cursor.get("seq")
    if not (_is_seq(seq) and seq == page_seq >= 1):
        raise ValueError(f"peer cursor seq {seq!r} does not match the page seq {page_seq!r}")
    anchor, fractions = cursor.get("anchor"), {key: cursor.get(key) for key in FRACTION_KEYS}
    if not isinstance(anchor, str) or not all(_is_number(v) for v in fractions.values()):
        raise TypeError(f"peer cursor has no anchor/{'/'.join(FRACTION_KEYS)}")
    validate_position(anchor, {key: float(v) for key, v in fractions.items() if _is_number(v)})


def parse_peer_latest(page: Any) -> dict[str, Any]:
    """Validate the peer's ``/cursor/latest`` answer; raises TypeError/ValueError on a bad page."""
    if not isinstance(page, dict) or not isinstance(page.get("epoch"), str) or not _is_seq(page.get("seq")):
        raise TypeError("peer cursor page has no epoch/seq")
    if not isinstance(page.get("machine"), str) or not page["machine"]:
        raise TypeError("peer cursor page has no machine name")
    if "cursor" not in page:
        raise TypeError("peer cursor page has no cursor field (null when nothing was sent)")
    cursor = page["cursor"]
    # Slot invariant: seq 0 means nothing sent (no cursor); seq N > 0 always carries cursor N.
    if cursor is None and page["seq"] != 0:
        raise ValueError(f"peer cursor page has seq {page['seq']} but no cursor")
    if cursor is not None:
        _check_peer_cursor(cursor, page["seq"])
    return {"machine": page["machine"], "epoch": page["epoch"], "seq": page["seq"], "cursor": cursor}
