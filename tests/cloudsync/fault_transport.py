"""A fault-injecting :class:`apps.sync_hub.client.HubTransport` (plan W9).

Wraps any real transport (in this suite, the ``TestClient``-backed one that
drives the real hub router) and injects the network failures the protocol
claims to survive. It fakes no hub behavior. Every call that is allowed
through reaches the real router and commits exactly as it would in
production. The only thing that ever changes is whether the request is sent
and whether its answer comes back.

Fault modes, one per real failure class:

* ``fail_before_send`` -- the connection died before the hub saw the
  request. The hub state is untouched.
* ``drop_response_after_commit`` -- the hub received the request and
  COMMITTED it, then the answer was lost (a timeout, a reset, a laptop lid
  closing). This is the most common real network failure, and the only one
  where the two sides disagree about what happened.
* ``tamper_response`` -- the hub committed and answered, and a field of the
  answer is rewritten in flight. It models a hub build that answers a
  question differently from this one, for example a hub on another schema
  version that lacks the ``/hello`` gate. It is the one mode that alters an
  output, so it is kept separate and named for what it does.

Every fault is armed against the Nth matching call, optionally restricted to
one endpoint. A fault that never fires is a test that measured nothing, so
:meth:`FaultTransport.assert_all_fired` raises on any fault still pending.
Every call is recorded in :attr:`FaultTransport.calls`, which is what lets a
test say "no push was ever sent" as a presence check on the log rather than
an absence inferred from silence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from apps.sync_hub import client

FaultMode = Literal["fail_before_send", "drop_response_after_commit", "tamper_response"]
CallOutcome = Literal[
    "delivered", "failed_before_send", "dropped_after_commit", "tampered", "hub_error"
]
ResponseTamper = Callable[[dict[str, Any]], dict[str, Any]]

FAULT_MODES: tuple[FaultMode, ...] = (
    "fail_before_send",
    "drop_response_after_commit",
    "tamper_response",
)


class FaultInjected(client.SyncTransportError):
    """A failure this harness injected on purpose.

    A subclass of the real :class:`~apps.sync_hub.client.SyncTransportError`,
    so production code sees exactly the category a dead network produces,
    while a test can still tell an injected fault from a real one.
    """


@dataclass
class _Fault:
    mode: FaultMode
    path: str | None
    nth: int
    tamper: ResponseTamper | None = None
    seen: int = 0
    fired: bool = False
    preempted: bool = False

    def matches(self, path: str) -> bool:
        return self.path is None or path.endswith(self.path)

    def describe(self) -> str:
        where = f"call #{self.nth} to {self.path}" if self.path else f"call #{self.nth}"
        suffix = " (preempted: the hub itself errored on that call)" if self.preempted else ""
        return f"{self.mode} on {where}{suffix}"


@dataclass(frozen=True)
class CallRecord:
    """One call through the transport and what happened to it."""

    method: str
    path: str
    outcome: CallOutcome

    @property
    def reached_hub(self) -> bool:
        return self.outcome != "failed_before_send"


def _endpoint(path: str) -> str:
    """``push`` or ``/push`` -> ``/push``; a suffix every sync path ends with."""
    if not path:
        raise ValueError("a fault path must name an endpoint, e.g. 'push'")
    return "/" + path.lstrip("/")


class FaultTransport:
    """A :class:`~apps.sync_hub.client.HubTransport` that fails on request."""

    def __init__(self, inner: client.HubTransport) -> None:
        self._inner = inner
        self._faults: list[_Fault] = []
        self.calls: list[CallRecord] = []

    # ----- arming ------------------------------------------------------------

    def _arm(
        self,
        mode: FaultMode,
        *,
        nth: int,
        path: str | None,
        tamper: ResponseTamper | None = None,
    ) -> None:
        if nth < 1:
            raise ValueError(f"nth is 1-based, got {nth}")
        if mode not in FAULT_MODES:
            raise ValueError(f"unknown fault mode {mode!r}; expected one of {FAULT_MODES}")
        if (mode == "tamper_response") != (tamper is not None):
            raise ValueError("a tamper function is required by, and only by, tamper_response")
        self._faults.append(
            _Fault(
                mode=mode,
                path=None if path is None else _endpoint(path),
                nth=nth,
                tamper=tamper,
            )
        )

    def fail_before_send(self, nth: int = 1, *, path: str | None = None) -> None:
        """The ``nth`` matching call never reaches the hub."""
        self._arm("fail_before_send", nth=nth, path=path)

    def drop_response_after_commit(self, nth: int = 1, *, path: str | None = None) -> None:
        """The ``nth`` matching call reaches the hub and commits; its answer is lost."""
        self._arm("drop_response_after_commit", nth=nth, path=path)

    def fail_on_nth(self, path: str, nth: int, *, mode: FaultMode = "fail_before_send") -> None:
        """Arm ``mode`` against the ``nth`` call to one endpoint."""
        if mode == "tamper_response":
            raise ValueError("use tamper_response() so the tamper function is explicit")
        self._arm(mode, nth=nth, path=path)

    def tamper_response(self, path: str, tamper: ResponseTamper, *, nth: int = 1) -> None:
        """The ``nth`` call to ``path`` commits, and its answer is rewritten."""
        self._arm("tamper_response", nth=nth, path=path, tamper=tamper)

    # ----- reading -------------------------------------------------------------

    @property
    def pending(self) -> list[str]:
        return [fault.describe() for fault in self._faults if not fault.fired]

    def assert_all_fired(self) -> None:
        """Raise if any armed fault never fired: that test measured nothing."""
        if self.pending:
            raise AssertionError(
                f"armed fault(s) never fired, so the failure under test never "
                f"happened: {self.pending}. Calls seen: {self.calls}"
            )

    def calls_to(self, path: str) -> list[CallRecord]:
        endpoint = _endpoint(path)
        return [call for call in self.calls if call.path.endswith(endpoint)]

    # ----- the transport -------------------------------------------------------

    def _due(self, path: str) -> _Fault | None:
        """Count this call against every matching fault; return the one that is due.

        A due fault is NOT marked fired here. It is fired only by the branch
        in :meth:`_call` that actually produces its outcome, so a call the hub
        itself refused leaves the fault pending (and preempted) rather than
        letting ``assert_all_fired`` pass on a failure that never happened.
        """
        due: _Fault | None = None
        for fault in self._faults:
            if fault.fired or not fault.matches(path):
                continue
            fault.seen += 1
            if fault.seen == fault.nth and due is None:
                due = fault
        return due

    def _call(self, method: str, path: str, send: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        fault = self._due(path)
        label = f"{method} {path}"
        if fault is not None and fault.mode == "fail_before_send":
            fault.fired = True
            self.calls.append(CallRecord(method, path, "failed_before_send"))
            raise FaultInjected(f"{label}: injected {fault.describe()} (hub never saw it)")
        try:
            answer = send()
        except client.SyncTransportError:
            if fault is not None:
                fault.preempted = True
            self.calls.append(CallRecord(method, path, "hub_error"))
            raise
        if fault is None:
            self.calls.append(CallRecord(method, path, "delivered"))
            return answer
        if fault.mode == "drop_response_after_commit":
            fault.fired = True
            self.calls.append(CallRecord(method, path, "dropped_after_commit"))
            raise FaultInjected(f"{label}: injected {fault.describe()} (hub committed it)")
        if fault.mode == "tamper_response" and fault.tamper is not None:
            fault.fired = True
            self.calls.append(CallRecord(method, path, "tampered"))
            return fault.tamper(dict(answer))
        raise AssertionError(f"unhandled fault {fault.describe()}")

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("POST", path, lambda: self._inner.post(path, payload))

    def get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        return self._call("GET", path, lambda: self._inner.get(path, params))


__all__ = [
    "FAULT_MODES",
    "CallRecord",
    "FaultInjected",
    "FaultMode",
    "FaultTransport",
]
