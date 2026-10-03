"""The gates in front of every Sentry send: live set, tracing, and the sink.

Split out of test_telemetry.py (file-size ratchet). No network and no live
Sentry project: every delivery assertion uses sentry_sdk's in-memory
transport, the SDK's own way to assert on captured envelopes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.shared.telemetry import (
    DSN_ENV,
    TELEMETRY_ENV,
    TRACES_SAMPLE_RATE_ENV,
    TelemetryConfigError,
    capture_browser_error,
    decide_telemetry,
    init_telemetry,
    mirror_transport_live,
    reset_live_gate_for_tests,
    set_live_transport_probe,
    transport_is_live,
)
from tests.support.sentry_client import close_sentry_client

pytestmark = pytest.mark.no_leaked_threads

DSN = "https://public@o0.ingest.de.sentry.io/1"
SHA = "0123456789abcdef0123456789abcdef01234567"


def _raise_deliberate_failure() -> None:
    raise ValueError("deliberate: /Users/dev/Music/Nina Vale - Marigold.mp3 failed to decode")


# ----- live-set gate (never send while a deck is playing) -------------------
class _CapturingTransport:
    """Built lazily so the module import stays SDK-free until a test needs it."""

    @staticmethod
    def make(captured: list[dict]):  # type: ignore[no-untyped-def]
        from sentry_sdk.transport import Transport

        class CapturingTransport(Transport):
            def capture_envelope(self, envelope) -> None:  # type: ignore[no-untyped-def]
                captured.extend(
                    item.payload.json
                    for item in envelope.items
                    if item.payload.json is not None
                )

        return CapturingTransport()


@pytest.fixture()
def live_gate_reset():
    reset_live_gate_for_tests()
    yield
    reset_live_gate_for_tests()


def _mirror(
    *, playing: bool = False, audible: bool = False, age_s: float = 0.0, now: datetime
) -> dict:
    stamp = now - timedelta(seconds=age_s)
    return {
        "received_at": stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "decks": {
            "1": {"playing": playing, "audible": audible},
            "2": {"playing": False, "audible": False},
        },
    }


# Cases as kwargs, and the mirror built INSIDE the test against an explicit
# clock. The first version built the mirrors at collection time, so after an
# eight-minute suite the "playing" mirror was older than the 15 s staleness
# bound and the test failed for the right reason at the wrong moment.
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("no-page", False),
        ("idle", False),
        ("playing", True),
        ("audible", True),
        # Stale: a page that closed mid-track must not mute the engine forever.
        ("stale", False),
        ("unstamped", False),  # no received_at: unknown, not live
        ("bad-stamp", False),
    ],
)
def test_mirror_transport_live_reads_both_flags_and_refuses_stale(
    case: str, expected: bool
) -> None:
    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
    mirrors: dict[str, dict | None] = {
        "no-page": None,
        "idle": _mirror(now=now),
        "playing": _mirror(playing=True, now=now),
        "audible": _mirror(audible=True, now=now),
        "stale": _mirror(playing=True, age_s=60.0, now=now),
        "unstamped": {"decks": {"1": {"playing": True}}},
        "bad-stamp": {"received_at": "not-a-time", "decks": {"1": {"playing": True}}},
    }
    assert mirror_transport_live(mirrors[case], now=now) is expected


def test_probe_absent_or_raising_reads_not_live(live_gate_reset: None) -> None:
    """A broken probe must not silently mute every event: it reads NOT live."""
    assert transport_is_live() is False

    def explode() -> bool:
        raise RuntimeError("mirror store unreachable")

    set_live_transport_probe(explode)
    assert transport_is_live() is False
    set_live_transport_probe(lambda: True)
    assert transport_is_live() is True


def test_engine_exception_stays_local_while_a_deck_is_live(live_gate_reset: None) -> None:
    """if the probe says a deck is live then an engine exception never reaches
    the transport; once transport stops the next one does. Control in the
    same test: the idle case DOES deliver, so the gate is the thing under test
    rather than a transport that delivers nothing.
    """
    sentry_sdk = pytest.importorskip("sentry_sdk", reason="needs the optional observability extra")

    captured: list[dict] = []
    decision = decide_telemetry(
        {TELEMETRY_ENV: "1", DSN_ENV: DSN}, build_source="payload", release=SHA
    )
    live = {"value": True}
    set_live_transport_probe(lambda: live["value"])
    try:
        assert init_telemetry(decision, transport=_CapturingTransport.make(captured)) is True
        try:
            _raise_deliberate_failure()
        except ValueError as exc:
            sentry_sdk.capture_exception(exc)
        sentry_sdk.flush()
        assert captured == [], "a mid-mix exception left the machine"

        live["value"] = False
        try:
            _raise_deliberate_failure()
        except ValueError as exc:
            sentry_sdk.capture_exception(exc)
        sentry_sdk.flush()
    finally:
        close_sentry_client()
    assert len(captured) == 1, "the post-set exception must still be delivered"


def test_browser_flag_is_authoritative_over_the_engine_probe(live_gate_reset: None) -> None:
    """The page's own any_deck_live wins: True holds even when the mirror says
    idle, False sends even when the mirror says live, None defers to the mirror.
    """
    sentry_sdk = pytest.importorskip("sentry_sdk", reason="needs the optional observability extra")

    captured: list[dict] = []
    decision = decide_telemetry(
        {TELEMETRY_ENV: "1", DSN_ENV: DSN}, build_source="payload", release=SHA
    )
    probe = {"value": False}
    set_live_transport_probe(lambda: probe["value"])

    def forward(flag: bool | None) -> str | None:
        return capture_browser_error(
            message="deck 1 processor error",
            name="Error",
            stack=None,
            url="http://127.0.0.1:5173/performance",
            user_agent="Mozilla/5.0",
            context={"kind": "ui-error", "any_deck_live": flag},
        )

    try:
        assert init_telemetry(decision, transport=_CapturingTransport.make(captured)) is True
        assert forward(True) is None, "page says live, mirror says idle: hold"
        probe["value"] = True
        assert forward(None) is None, "page did not say, mirror says live: hold"
        assert forward(False) is not None, "page says idle, mirror says live: send"
        probe["value"] = False
        assert forward(None) is not None, "page did not say, mirror says idle: send"
        sentry_sdk.flush()
    finally:
        close_sentry_client()
    assert len(captured) == 2
    assert [event["extra"]["any_deck_live"] for event in captured] == [False, None]


# ----- tracing opt-in -------------------------------------------------------
def test_tracing_is_off_unless_the_operator_sets_a_rate() -> None:
    on = decide_telemetry({TELEMETRY_ENV: "1", DSN_ENV: DSN}, build_source="payload", release=SHA)
    assert on.traces_sample_rate == 0.0
    traced = decide_telemetry(
        {TELEMETRY_ENV: "1", DSN_ENV: DSN, TRACES_SAMPLE_RATE_ENV: "0.25"},
        build_source="payload",
        release=SHA,
    )
    assert traced.traces_sample_rate == 0.25


@pytest.mark.parametrize("raw", ["1.5", "-0.1", "lots", "nan"])
def test_tracing_rate_outside_the_unit_interval_is_refused(raw: str) -> None:
    with pytest.raises(TelemetryConfigError) as excinfo:
        decide_telemetry(
            {TELEMETRY_ENV: "0", TRACES_SAMPLE_RATE_ENV: raw},
            build_source="repo",
            release=None,
        )
    assert TRACES_SAMPLE_RATE_ENV in str(excinfo.value)


def test_transactions_are_scrubbed_and_held_while_live(live_gate_reset: None) -> None:
    """if tracing is on then a transaction reaches the transport with its span
    text scrubbed; if a deck is live it does not reach the transport at all.
    """
    sentry_sdk = pytest.importorskip("sentry_sdk", reason="needs the optional observability extra")

    captured: list[dict] = []
    decision = decide_telemetry(
        {TELEMETRY_ENV: "1", DSN_ENV: DSN, TRACES_SAMPLE_RATE_ENV: "1"},
        build_source="payload",
        release=SHA,
    )
    live = {"value": False}
    set_live_transport_probe(lambda: live["value"])
    try:
        assert init_telemetry(decision, transport=_CapturingTransport.make(captured)) is True
        with (
            sentry_sdk.start_transaction(op="http.server", name="GET /api/v1/tracks"),
            sentry_sdk.start_span(
                op="fs.read", name="read /Users/dev/Music/Nina Vale - Marigold.mp3"
            ) as span,
        ):
            span.set_data("track_title", "Nina Vale - Marigold")
            span.set_data("deck_id", "A")
        sentry_sdk.flush()
        assert len(captured) == 1, "the idle transaction must be delivered"

        live["value"] = True
        with sentry_sdk.start_transaction(op="http.server", name="GET /api/v1/tracks"):
            pass
        sentry_sdk.flush()
    finally:
        close_sentry_client()

    assert len(captured) == 1, "a transaction was sent while a deck was live"
    transaction = captured[0]
    assert transaction["type"] == "transaction"
    spans = transaction["spans"]
    assert spans, "the child span was lost"
    assert "Marigold" not in spans[0]["description"]
    assert spans[0]["description"].endswith("<path.mp3>")
    assert spans[0]["data"]["track_title"] == "[redacted]"
    assert spans[0]["data"]["deck_id"] == "A"

