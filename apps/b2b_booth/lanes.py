"""The two engine-to-engine lanes over the paired channel (B2BBOOTH-91), contract v3 "Engine-to-engine
channel". Each lane is one sender thread posting envelopes through an injected ``post`` (a door
POST; ``apps.b2b_booth`` never imports a transport, the web UI layer wires one in):

- ``OpsLane`` (reliable, ordered): messages leave in the order they were put, batched into one
  envelope per POST, one POST in flight; a failed POST is retried with the SAME batch first, so
  nothing is lost or reordered (the receiver drops what it already has).
- ``CtlLane`` (newest wins): one pending message per key (``ctl:<unit>|<ctl>``, ``pos:<deck>``,
  ``renew``); a newer value replaces an unsent older one, and one POST is in flight at a time, so
  at most one value per key is ever in flight and a stalled POST delays but never queues stale
  moves. A failed POST puts back only the keys nothing newer has replaced.

Every answered POST is also a clock sample (``on_answer(lane, answer, t0_us, t1_us)``).
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from time import monotonic
from typing import Any

from apps.b2b_booth.protocol import PROTOCOL_VERSION, Lane

#: post(lane, envelope, timeout_s) -> the partner's answer (parsed JSON); raises on any failure.
Post = Callable[[Lane, dict[str, Any], float], dict[str, Any]]
OnAnswer = Callable[[Lane, dict[str, Any], int, int], None]
OnError = Callable[[Lane, str], None]

OPS_TIMEOUT_S = 3.0
CTL_TIMEOUT_S = 1.5
#: One envelope stays well under the door's booth body cap and one relay frame.
MAX_ENVELOPE_BYTES = 48 * 1024
RETRY_BACKOFF_S = (0.02, 0.05, 0.1, 0.25, 0.5)


def envelope(epoch: str | None, sent_us: int, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "epoch": epoch, "sent_us": sent_us,
            "messages": [{k: v for k, v in m.items() if k != "epoch"} for m in messages]}


class _Lane:
    name: Lane
    timeout_s: float

    def __init__(self, post: Post, now_us: Callable[[], int], on_answer: OnAnswer, on_error: OnError,
                 timeout_scale: float = 1.0) -> None:
        #: A slower transport (the internet relay door) waits longer before calling a POST failed:
        #: a retry of a POST that is merely slow only adds load to the path that is already slow.
        self.timeout_s = type(self).timeout_s * timeout_scale
        self._post = post
        self._now_us = now_us
        self._on_answer = on_answer
        self._on_error = on_error
        self._cv = threading.Condition()
        self._stopping = False
        self.posts = 0
        self.failures = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self._thread = threading.Thread(target=self._run, name=f"b2b-booth-{self.name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=5)

    def _take(self) -> list[tuple[str, dict[str, Any]]]:
        raise NotImplementedError

    def _restore(self, batch: list[tuple[str, dict[str, Any]]]) -> None:
        raise NotImplementedError

    def _pending(self) -> bool:
        raise NotImplementedError

    def _run(self) -> None:
        failures = 0
        while True:
            with self._cv:
                while not self._stopping and not self._pending():
                    self._cv.wait()
                if self._stopping:
                    return
                batch = self._take()
            epoch = batch[0][1].get("epoch")
            t0 = self._now_us()
            try:
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
                answer = self._post(self.name, envelope(epoch, t0, [m for _, m in batch]), self.timeout_s)
            except Exception as exc:  # noqa: BLE001 - any transport failure is retried, and reported
                with self._cv:
                    self._restore(batch)
                self.failures += 1
                self._on_error(self.name, f"{type(exc).__name__}: {exc}")
                with self._cv:
                    self._cv.wait(RETRY_BACKOFF_S[min(failures, len(RETRY_BACKOFF_S) - 1)])
                failures += 1
                continue
            finally:
                self.in_flight -= 1
            failures = 0
            self.posts += 1
            self._acknowledged()
            self._on_answer(self.name, answer, t0, self._now_us())

    def status(self) -> dict[str, Any]:
        with self._cv:
            pending = self._pending_count()
        return {"posts": self.posts, "failures": self.failures, "pending": pending, "max_in_flight": self.max_in_flight}

    def _pending_count(self) -> int:
        raise NotImplementedError

    def _acknowledged(self) -> None:
        """A POST was answered (the OPS lane tracks what it still owes)."""


class OpsLane(_Lane):
    name: Lane = "ops"
    timeout_s = OPS_TIMEOUT_S

    def __init__(self, *args: Any) -> None:
        self._queue: deque[dict[str, Any]] = deque()
        #: monotonic() when the oldest unacknowledged message was put; None while nothing is owed.
        self._owed_since: float | None = None
        super().__init__(*args)

    def put(self, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        with self._cv:
            if self._owed_since is None:
                self._owed_since = monotonic()
            self._queue.extend(messages)
            self._cv.notify_all()

    def stuck_s(self) -> float:
        """How long the oldest message has waited for the partner's answer (0 when nothing is owed).
        A partner that keeps refusing a message (a protocol disagreement) shows up here, loudly."""
        with self._cv:
            return 0.0 if self._owed_since is None else monotonic() - self._owed_since

    def _acknowledged(self) -> None:
        with self._cv:
            self._owed_since = monotonic() if self._queue else None

    def _pending(self) -> bool:
        return bool(self._queue)

    def _pending_count(self) -> int:
        return len(self._queue)

    def _take(self) -> list[tuple[str, dict[str, Any]]]:
        """The longest run from the head with one epoch, under the envelope size cap (at least one)."""
        batch: list[tuple[str, dict[str, Any]]] = []
        size = 0
        epoch = self._queue[0].get("epoch")
        while self._queue and self._queue[0].get("epoch") == epoch:
            message_size = len(json.dumps(self._queue[0]))
            if batch and size + message_size > MAX_ENVELOPE_BYTES:
                break
            batch.append(("", self._queue.popleft()))
            size += message_size
        return batch

    def _restore(self, batch: list[tuple[str, dict[str, Any]]]) -> None:
        self._queue.extendleft(m for _, m in reversed(batch))


class CtlLane(_Lane):
    name: Lane = "ctl"
    timeout_s = CTL_TIMEOUT_S

    def __init__(self, *args: Any) -> None:
        self._latest: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.replaced = 0
        super().__init__(*args)

    def put(self, items: list[tuple[str, dict[str, Any]]]) -> None:
        if not items:
            return
        with self._cv:
            for key, message in items:
                if key in self._latest:
                    self.replaced += 1
                    del self._latest[key]
                self._latest[key] = message
            self._cv.notify_all()

    def _pending(self) -> bool:
        return bool(self._latest)

    def _pending_count(self) -> int:
        return len(self._latest)

    def _take(self) -> list[tuple[str, dict[str, Any]]]:
        epoch = next(iter(self._latest.values())).get("epoch")
        batch = [(k, m) for k, m in self._latest.items() if m.get("epoch") == epoch]
        for key, _ in batch:
            del self._latest[key]
        return batch

    def _restore(self, batch: list[tuple[str, dict[str, Any]]]) -> None:
        for key, message in batch:
            self._latest.setdefault(key, message)  # a newer value put meanwhile wins

    def status(self) -> dict[str, Any]:
        return {**super().status(), "replaced": self.replaced}
