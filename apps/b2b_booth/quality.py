"""B2BBOOTH-98: the booth quality tap's drains, held by the engine for the real-Mac report.

Contract v3 "Engine surface": ``POST|GET /api/v1/b2b/booth/quality`` (quality builds only; the
route is flag-gated and the POST is this laptop's page only). The page opened with
``?booth_quality=1`` loads the S4 tap (``static/booth_tap.js``) and posts what ``drain()`` returns;
``python -m scripts.booth_alignment_report real`` GETs it back from both engines
(docs/b2b-link/BOOTH-QUALITY.md, "Wiring the real booth" 3).

A drain is ``{taps: [{label, sample_rate, state, chunks: [{frame, pcm}], stamps: [...]}], inputs,
shown, pings, cue_tap?}``. The tap's own semantics decide the merge: ``drain()`` returns every
chunk, stamp, input and shown sample recorded since ``start()`` (so successive drains repeat
them) but splices its pings out (so each drain carries only new ones). The engine therefore keeps
a bounded ring of posted drains and merges them on read, keeping each distinct record once (identity
is the whole record: a stalled audio clock makes the tap emit different chunks with the same frame,
seen in a recorded S4 rig bundle). A post that would overflow the ring evicts the
OLDEST posts first and the read says how many were evicted (never a silent truncation).
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from apps.b2b_booth.protocol import BoothError

#: One posted drain is at most this (a 60 s capture of one tap at 48 kHz is about 7.7 MB of base64).
MAX_POST_BYTES = 16 * 1024 * 1024
#: The ring holds at most this many bytes of posted drains, and this many posts.
MAX_RING_BYTES = 48 * 1024 * 1024
MAX_RING_POSTS = 64
DRAIN_KEYS = frozenset({"taps", "inputs", "shown", "pings", "cue_tap"})
TAP_KEYS = frozenset({"label", "sample_rate", "state", "chunks", "stamps"})


def _list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise BoothError("bad_field", f"{where} must be a list")
    return value


def validate_drain(body: Any) -> dict[str, Any]:
    """The tap's ``drain()`` shape (what booth_tap.js returns), or ``BoothError`` naming what is wrong."""
    if not isinstance(body, dict):
        raise BoothError("bad_field", "a quality post is the tap's drain() object")
    extra = sorted(set(body) - DRAIN_KEYS)
    if extra:
        raise BoothError("extra_keys", f"unexpected keys in a quality drain: {extra}")
    if "taps" not in body:
        raise BoothError("bad_field", "a quality drain needs taps (the tap's drain() always has them)")
    for tap in _list(body["taps"], "taps"):
        if not isinstance(tap, dict) or not isinstance(tap.get("label"), str) or set(tap) - TAP_KEYS:
            raise BoothError("bad_field", f"every tap is {{{', '.join(sorted(TAP_KEYS))}}} with a string label")
        for chunk in _list(tap.get("chunks", []), "taps[].chunks"):
            if not isinstance(chunk, dict) or not isinstance(chunk.get("frame"), int) or not isinstance(chunk.get("pcm"), str):
                raise BoothError("bad_field", "every chunk is {frame: int, pcm: base64 string}")
        _list(tap.get("stamps", []), "taps[].stamps")
    for key in ("inputs", "shown", "pings"):
        _list(body.get(key, []), key)
    cue = body.get("cue_tap")
    if cue is not None and not isinstance(cue, str):
        raise BoothError("bad_field", "cue_tap is a tap label or null")
    return body


@dataclass
class _Post:
    at: float
    size: int
    drain: dict[str, Any]


def _dedupe(records: list[dict[str, Any]], order: str) -> list[dict[str, Any]]:
    """Each distinct record once (the tap re-sends identical ones in every drain), in ``order`` (a
    numeric field, ties keep the tap's own order). Identity is the WHOLE record, never one field: a
    stalled audio clock makes the tap emit several different chunks stamped with the same frame."""
    seen: dict[str, dict[str, Any]] = {}
    for record in records:
        seen.setdefault(json.dumps(record, sort_keys=True), record)
    return sorted(seen.values(), key=lambda r: (r.get(order) is not None, r.get(order) or 0))


class QualityRing:
    """Bounded ring of posted drains; ``merged()`` is what the report reads."""

    def __init__(self, max_bytes: int = MAX_RING_BYTES, max_posts: int = MAX_RING_POSTS) -> None:
        self.max_bytes, self.max_posts = max_bytes, max_posts
        self._posts: deque[_Post] = deque()
        self._bytes = 0
        self._lock = threading.Lock()
        self.posted = 0
        self.evicted = 0

    def post(self, drain: dict[str, Any], size: int) -> dict[str, Any]:
        if size > self.max_bytes:
            raise BoothError("too_large", f"a quality drain of {size} bytes is over the ring's {self.max_bytes}", 413)
        validate_drain(drain)
        with self._lock:
            self._posts.append(_Post(time.time(), size, drain))
            self._bytes += size
            self.posted += 1
            while self._bytes > self.max_bytes or len(self._posts) > self.max_posts:
                gone = self._posts.popleft()
                self._bytes -= gone.size
                self.evicted += 1
            return self._ring_locked()

    def clear(self) -> dict[str, Any]:
        with self._lock:
            self._posts.clear()
            self._bytes = 0
            return self._ring_locked()

    def _ring_locked(self) -> dict[str, Any]:
        return {"posts": len(self._posts), "bytes": self._bytes, "posted": self.posted, "evicted_posts": self.evicted,
                "max_bytes": self.max_bytes, "max_posts": self.max_posts,
                "first_at": self._posts[0].at if self._posts else None, "last_at": self._posts[-1].at if self._posts else None}

    def merged(self) -> dict[str, Any]:
        """Every kept post merged into one drain (the report's input), plus ``ring`` (what was kept)."""
        with self._lock:
            posts = [p.drain for p in self._posts]
            ring = self._ring_locked()
        taps: dict[str, dict[str, Any]] = {}
        inputs: list[dict[str, Any]] = []
        shown: list[dict[str, Any]] = []
        pings: list[dict[str, Any]] = []
        cue_tap: str | None = None
        for drain in posts:
            for tap in drain["taps"]:
                kept = taps.setdefault(tap["label"], {"label": tap["label"], "chunks": [], "stamps": []})
                kept["sample_rate"], kept["state"] = tap.get("sample_rate"), tap.get("state")
                kept["chunks"] += tap.get("chunks", [])
                kept["stamps"] += tap.get("stamps", [])
            inputs += drain.get("inputs", [])
            shown += drain.get("shown", [])
            pings += drain.get("pings", [])
            cue_tap = drain.get("cue_tap", cue_tap)
        for tap in taps.values():
            tap["chunks"] = _dedupe(tap["chunks"], "frame")
            tap["stamps"] = _dedupe(tap["stamps"], "now_ms")
        return {"taps": list(taps.values()), "inputs": _dedupe(inputs, "now_ms"),
                "shown": _dedupe(shown, "now_ms"), "pings": _dedupe(pings, "sent_ms"),
                "cue_tap": cue_tap, "ring": ring}
