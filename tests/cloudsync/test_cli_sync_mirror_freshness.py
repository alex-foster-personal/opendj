"""CLOUDSYNC-14: the CLI mirror probe's pure freshness and loopback-refusal rules.

Split out of `test_cli_sync_live_deck_gate.py` (PR #3831, quality ratchet: the file
crossed the 600-line limit). These exercise `_ui_mirror_is_fresh`,
`_ui_mirror_received_at` and `_refused_by_loopback_engine` directly, with no engine;
the end-to-end probe scenarios against the real engine stay in the original file.

[if] a mirror body is stale, naive, future-dated or missing its timestamp [then] it is
not trusted as fresh, [else stop].
[if] a connection refusal is not a verified loopback refusal [then] it is not read as
safely absent, [else stop].

-Claude
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from apps.sync_hub import maintenance
from tests.cloudsync.live_engine_rig import (
    free_port,
)

pytestmark = pytest.mark.requirement("CLOUDSYNC-14")


@pytest.mark.parametrize(
    ("host", "exc", "expected"),
    [
        ("127.0.0.1", ConnectionRefusedError(61, "refused"), True),
        ("::1", ConnectionRefusedError(61, "refused"), True),
        ("localhost", ConnectionRefusedError(61, "refused"), True),
        ("127.1", ConnectionRefusedError(61, "refused"), False),
        ("10.0.0.5", ConnectionRefusedError(61, "refused"), False),
        ("example.com", ConnectionRefusedError(61, "refused"), False),
        ("127.0.0.1", None, False),
        ("127.0.0.1", TimeoutError("timed out"), False),
        ("127.0.0.1", OSError(0, "no errno"), False),
    ],
)
def test_refused_by_loopback_engine_requires_both_conditions(
    host: str, exc: BaseException | None, expected: bool
) -> None:
    """Direct unit coverage of the P1 fix (Sol review, PR #3831): neither a
    non-loopback host nor a non-ECONNREFUSED failure may read as safe, no
    matter how plausible either looks alone."""
    assert maintenance._refused_by_loopback_engine(host, exc) is expected


def test_refused_by_loopback_engine_unwraps_the_real_httpx_httpcore_chain() -> None:
    """Reproduces the REAL exception shape httpx/httpcore produce for a
    genuine loopback refusal, rather than a synthetic approximation, so the
    unwrap logic is proven against the actual nesting httpx puts between
    `_probe_engine_lock`'s ``exc.__cause__`` and the OS-level
    ``ConnectionRefusedError`` two layers down.
    """
    port = free_port()  # nothing bound
    try:
        with httpx.Client(timeout=2.0) as probe_client:
            probe_client.get(f"http://127.0.0.1:{port}/")
    except httpx.ConnectError as caught:
        real_cause = caught
    else:  # pragma: no cover -- would mean the OS stopped refusing loopback
        raise AssertionError("expected a ConnectError against an unbound port")
    assert maintenance._refused_by_loopback_engine("127.0.0.1", real_cause) is True
    assert maintenance._refused_by_loopback_engine("127.1", real_cause) is False


@pytest.mark.parametrize("received_at", [None, "", "not-a-timestamp", 12345, [1, 2]])
def test_ui_mirror_is_fresh_false_for_missing_or_invalid_received_at(
    received_at: Any,
) -> None:
    """[if] ``received_at`` is missing, empty, unparseable, or the wrong type
    [then] the body reads as NOT fresh -- fail closed on an invariant that
    should always hold for a real server-stamped body, per this repo's
    "verify the presence of the good thing" rule."""
    assert maintenance._ui_mirror_is_fresh({"received_at": received_at}) is False


def test_ui_mirror_is_fresh_false_when_received_at_key_is_absent() -> None:
    assert maintenance._ui_mirror_is_fresh({}) is False


def test_ui_mirror_is_fresh_true_just_inside_the_tolerance() -> None:
    now = datetime.now(UTC)
    received = now - timedelta(seconds=maintenance._UI_MIRROR_FRESHNESS_TOLERANCE_S - 0.5)
    body = {"received_at": received.isoformat(timespec="milliseconds").replace("+00:00", "Z")}
    assert maintenance._ui_mirror_is_fresh(body, now=now) is True


def test_ui_mirror_is_fresh_false_just_outside_the_tolerance() -> None:
    now = datetime.now(UTC)
    received = now - timedelta(seconds=maintenance._UI_MIRROR_FRESHNESS_TOLERANCE_S + 0.5)
    body = {"received_at": received.isoformat(timespec="milliseconds").replace("+00:00", "Z")}
    assert maintenance._ui_mirror_is_fresh(body, now=now) is False


# ----- CLOUDSYNC-14 round 3, finding 2: naive and future timestamps --------


def test_ui_mirror_received_at_rejects_a_naive_timestamp() -> None:
    """[if] ``received_at`` parses but carries no timezone [then] it reads as
    unparseable, not as an assumed UTC (Sol review, PR #3831, P1/BLOCKING):
    the real route always stamps an explicit UTC offset, so a naive stamp is
    never something a genuine 200 can produce, and silently assuming UTC for
    one fabricates a timestamp nothing verified."""
    naive = "2026-09-25T10:00:00.000000"
    assert maintenance._ui_mirror_received_at({"received_at": naive}) is None


def test_ui_mirror_is_fresh_false_for_a_naive_timestamp_even_within_tolerance() -> None:
    """Same as above, through the caller a malformed response actually
    reaches: a naive stamp that LOOKS recent must still defer, not pass."""
    now = datetime.now(UTC)
    naive_now = now.replace(tzinfo=None).isoformat(timespec="milliseconds")
    assert maintenance._ui_mirror_is_fresh({"received_at": naive_now}, now=now) is False


def test_ui_mirror_is_fresh_false_for_a_future_timestamp() -> None:
    """[if] ``received_at`` is in the future [then] the body reads as NOT
    fresh (Sol review, PR #3831, P1/BLOCKING): ``current - received_at`` is
    NEGATIVE for a future stamp, and a bare ``<= tolerance`` check let any
    negative age through -- a malformed response or a backward clock jump on
    the engine could make a stale idle snapshot look freshly stamped. Ten
    minutes ahead is used so this cannot be mistaken for ordinary clock
    skew across a tolerance measured in single-digit seconds."""
    now = datetime.now(UTC)
    future = now + timedelta(minutes=10)
    body = {"received_at": future.isoformat(timespec="milliseconds").replace("+00:00", "Z")}
    assert maintenance._ui_mirror_is_fresh(body, now=now) is False


def test_ui_mirror_is_fresh_true_at_exactly_zero_age() -> None:
    """Boundary check for the ``[0, tolerance]`` range: a stamp exactly AT
    ``now`` (age 0) is fresh, confirming the fix did not flip the inclusive
    lower bound into an exclusive one while closing the future-timestamp
    gap."""
    now = datetime.now(UTC)
    body = {"received_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z")}
    assert maintenance._ui_mirror_is_fresh(body, now=now) is True
