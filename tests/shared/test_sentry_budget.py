"""Sentry quota guard: ids that collapse, a per-id and per-day budget, no perf mirrors.

[if] one process repeats an error past its budget [then] Sentry sees only the budget, [else stop].

Measured on the Air's error sink, Sun 13 Sep 2026: one machine produced 1,374
events in a day against a 50k/month quota (about 1,667 a day across EVERY
host). 42% were console mirrors of perf events, and numbers glued to units
("2919ms") split one fault into dozens of ids. Round 1 of
specs/sentry-quota-spec.md replays that day at 100. The local JSONL sink still
records every event; only what leaves the machine is budgeted.

Regression lines:
  - if two messages differing only in a unit-suffixed number get different ids, then broken
  - if one error id reaches Sentry more than 3 times in an hour, then broken
  - if one process sends more than 100 events in a UTC day, then broken
  - if a console mirror of a perf event reaches Sentry, then broken
  - if SENTRY_ENVIRONMENT is overridden to dev/ship, then broken
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.shared.telemetry import (
    DSN_ENV,
    ENVIRONMENT_ENV,
    TELEMETRY_ENV,
    TelemetryConfigError,
    capture_browser_error,
    decide_telemetry,
    init_telemetry,
    scrub_event,
)
from apps.shared.telemetry.budget import (
    SENTRY_BUDGET,
    SENTRY_EVENTS_PER_DAY,
    SENTRY_EVENTS_PER_ID_PER_HOUR,
    TRANSIENT_NETWORK_MESSAGES,
    SentryBudget,
    is_dev_tooling_console,
    is_local_only_browser_error,
    is_perf_console_mirror,
    is_transient_network_error,
)
from apps.shared.telemetry.error_id import stable_error_id
from tests.support.sentry_client import close_sentry_client

pytestmark = [pytest.mark.requirement("OBS-01"), pytest.mark.no_leaked_threads]

DSN = "https://public@o0.ingest.de.sentry.io/1"
SHA = "0123456789abcdef0123456789abcdef01234567"
SITE = "client:ui-error:/performance"
HOUR_S = 3600.0
DAY_S = 86400.0


class _Clock:
    """An injected clock, not a patch: SentryBudget takes it as a parameter."""

    def __init__(self) -> None:
        self.now = 1_789_257_600.0  # Sun 13 Sep 2026 00:00:00Z

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _fresh_global_budget() -> Iterator[None]:
    SENTRY_BUDGET.reset()
    yield
    SENTRY_BUDGET.reset()


# ----- ids ----------------------------------------------------------------
def test_unit_suffixed_numbers_share_an_id() -> None:
    """if 2919ms and 4497ms of the same stall get different ids then broken."""
    first = "audio-output-stalled: frozen for 2919ms (>= 2000ms) while playing"
    second = "audio-output-stalled: frozen for 4497ms (>= 2000ms) while playing"
    assert stable_error_id(source_site=SITE, message=first) == stable_error_id(
        source_site=SITE, message=second
    )


def test_decimal_numbers_share_an_id() -> None:
    """if a 7.5ms and a 12.5ms threshold split one xrun into two ids then broken."""
    first = "xrun: 1 xrun(s) in 2000ms; worst gap 11ms against a 7.5ms threshold"
    second = "xrun: 3 xrun(s) in 2052ms; worst gap 63ms against a 12.5ms threshold"
    assert stable_error_id(source_site=SITE, message=first) == stable_error_id(
        source_site=SITE, message=second
    )


def test_different_faults_still_get_different_ids() -> None:
    """Mutation control: collapsing numbers must not collapse distinct faults."""
    assert stable_error_id(
        source_site=SITE, message="audio-output-stalled for 10ms"
    ) != stable_error_id(source_site=SITE, message="audio-output-dead for 10ms")


# ----- budget -------------------------------------------------------------
def test_one_error_id_is_capped_per_hour_and_recovers() -> None:
    """if one id reaches Sentry more than 3 times in an hour then broken."""
    clock = _Clock()
    budget = SentryBudget(per_id_per_hour=3, per_day=100, clock=clock)
    verdicts = [budget.admit("eid-aaaaaaaaaaaa") for _ in range(5)]
    assert verdicts == [True, True, True, False, False]
    assert budget.admit("eid-bbbbbbbbbbbb") is True, "another id has its own budget"
    clock.now += HOUR_S
    assert budget.admit("eid-aaaaaaaaaaaa") is True, "the window must roll"
    assert budget.suppressed == 2


def test_one_process_is_capped_per_utc_day_and_recovers() -> None:
    """if one process sends more than 100 events in a UTC day then broken."""
    clock = _Clock()
    budget = SentryBudget(per_id_per_hour=3, per_day=100, clock=clock)
    admitted = sum(budget.admit(f"eid-{n:012x}") for n in range(150))
    assert admitted == 100
    clock.now += DAY_S
    assert budget.admit("eid-cccccccccccc") is True, "the day must roll"


def test_shipped_limits_match_the_measured_round() -> None:
    """The replayed round-1 numbers are the shipped ones, not a looser guess."""
    assert (SENTRY_EVENTS_PER_ID_PER_HOUR, SENTRY_EVENTS_PER_DAY) == (3, 100)


# ----- perf-event console mirrors -----------------------------------------
@pytest.mark.parametrize(
    ("kind", "message", "expected"),
    [
        ("console-warn", "[perf-event] xrun: 2 xrun(s) in 2000ms", True),
        ("console-error", "[perf-event] audio-output-stalled: frozen", True),
        ("ui-error", "[perf-event] xrun: 2 xrun(s) in 2000ms", False),
        ("console-error", "TypeError: x is undefined", False),
    ],
)
def test_perf_console_mirror_predicate(kind: str, message: str, expected: bool) -> None:
    """if a console copy of a perf event is not recognised then broken."""
    assert is_perf_console_mirror(kind, message) is expected


# ----- end to end through the SDK's own transport -------------------------
@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[list[dict]]:
    """A real init (real before_send, real budget) with an in-memory transport."""
    sentry_sdk = pytest.importorskip("sentry_sdk", reason="needs the optional observability extra")
    from sentry_sdk.transport import Transport

    monkeypatch.setenv("OPENDJ_ERROR_SINK_LOG", str(tmp_path / "sink.jsonl"))
    envelopes: list[dict] = []

    class CapturingTransport(Transport):
        def capture_envelope(self, envelope) -> None:  # type: ignore[no-untyped-def]
            envelopes.extend(
                item.payload.json for item in envelope.items if item.payload.json is not None
            )

    decision = decide_telemetry(
        {TELEMETRY_ENV: "1", DSN_ENV: DSN}, build_source="payload", release=SHA
    )
    try:
        assert init_telemetry(decision, transport=CapturingTransport()) is True
        yield envelopes
        sentry_sdk.flush()
    finally:
        close_sentry_client()


def _raise_the_same_failure() -> None:
    raise RuntimeError("deliberate repeat: stalled for 2500ms")


def test_repeated_engine_exception_reaches_sentry_three_times(
    captured: list[dict],
) -> None:
    """if five identical engine exceptions all reach Sentry then broken."""
    sentry_sdk = pytest.importorskip("sentry_sdk", reason="needs the optional observability extra")

    for _ in range(5):
        try:
            _raise_the_same_failure()
        except RuntimeError as exc:
            sentry_sdk.capture_exception(exc)
    sentry_sdk.flush()
    assert len(captured) == 3


def test_console_perf_mirror_stays_local_but_escalation_reaches_sentry(
    captured: list[dict], tmp_path: Path
) -> None:
    """if a console mirror reaches Sentry, or the real escalation does not, then broken."""
    sentry_sdk = pytest.importorskip("sentry_sdk", reason="needs the optional observability extra")

    url = "http://127.0.0.1:9448/performance"
    mirror = capture_browser_error(
        message="[perf-event] xrun: 2 xrun(s) in 2000ms",
        name="Error",
        stack=None,
        url=url,
        user_agent="Mozilla/5.0",
        context={"kind": "console-warn"},
    )
    escalated = capture_browser_error(
        message="xrun: 2 xrun(s) in 2000ms",
        name="Error",
        stack=None,
        url=url,
        user_agent="Mozilla/5.0",
        context={"kind": "ui-error", "perf_kind": "xrun"},
    )
    sentry_sdk.flush()
    assert mirror is None
    assert escalated is not None
    assert len(captured) == 1
    rows = [json.loads(line) for line in (tmp_path / "sink.jsonl").read_text().splitlines()]
    assert len(rows) == 2, "the local sink must still record the mirror"


# ----- environment --------------------------------------------------------
def test_sentry_environment_is_honoured_not_overridden() -> None:
    """if SENTRY_ENVIRONMENT=preview is reported as dev then broken."""
    decision = decide_telemetry(
        {TELEMETRY_ENV: "1", DSN_ENV: DSN, ENVIRONMENT_ENV: "preview"},
        build_source="repo",
        release=SHA,
    )
    assert decision.environment == "preview"


def test_unknown_sentry_environment_is_refused() -> None:
    """if a typo like 'prod' silently becomes an environment then broken."""
    with pytest.raises(TelemetryConfigError, match=ENVIRONMENT_ENV):
        decide_telemetry(
            {TELEMETRY_ENV: "1", DSN_ENV: DSN, ENVIRONMENT_ENV: "prod"},
            build_source="repo",
            release=SHA,
        )


# ----- round 2: no log-derived duplicates, logentry scrubbed, dev tooling local
# Measured in the live org Mon 14 Sep 2026: the top issue by volume (92 of 219
# events) was the client-errors route's own log.error line, sent a second time
# by Sentry's default LoggingIntegration with an unscrubbed logentry path.
def test_logged_error_becomes_a_breadcrumb_not_an_event(captured: list[dict]) -> None:
    """if a log.error line reaches Sentry as its own event then broken."""
    import logging

    sentry_sdk = pytest.importorskip("sentry_sdk", reason="needs the optional observability extra")

    logging.getLogger("apps.webui.server.routes.client_errors").error(
        "browser error %s: %s", "abc123", "decode failed"
    )
    sentry_sdk.flush()
    assert captured == []


def test_logentry_is_scrubbed() -> None:
    """if a logentry carries a library path unscrubbed then broken."""
    path = "/Users/dev/Music/Nina Vale - Marigold.mp3"
    event = {
        "logentry": {
            "message": "decode failed %s",
            "params": [path],
            "formatted": f"decode failed {path}",
        }
    }
    scrubbed = scrub_event(event)
    assert scrubbed is not None
    assert "Marigold" not in json.dumps(scrubbed)


@pytest.mark.parametrize(
    ("kind", "message", "expected"),
    [
        ("console-error", "[hmr] Failed to reload /src/lib/components/rb/Deck.svelte.", True),
        ("console-warn", "[vite] server connection lost. Polling for restart...", True),
        ("ui-error", "[hmr] Failed to reload /src/lib/components/rb/Deck.svelte.", False),
        ("console-error", "TypeError: Failed to fetch", False),
    ],
)
def test_dev_tooling_console_predicate(kind: str, message: str, expected: bool) -> None:
    """if a Vite dev-server console line is not recognised as local-only then broken."""
    assert is_dev_tooling_console(kind, message) is expected


@pytest.mark.requirement("OBS-07")
@pytest.mark.parametrize(
    ("name", "message", "expected"),
    [
        ("TypeError", "Load failed", True),
        ("TypeError", "Failed to fetch", True),
        (None, "TypeError: Load failed", True),
        (None, "TypeError: Failed to fetch", True),
        ("TypeError", "NetworkError when attempting to fetch resource.", True),
        ("AbortError", "The user aborted a request.", True),
        (
            "TypeError",
            "Failed to fetch dynamically imported module: /assets/Deck.js",
            False,
        ),
        ("TypeError", "x is undefined", False),
    ],
)
def test_transient_network_predicate(name: str | None, message: str, expected: bool) -> None:
    """[if] Safari Load failed is shipped to Sentry [then] broken, [else stop]."""
    assert is_transient_network_error(name, message) is expected
    assert is_local_only_browser_error("unhandled-rejection", name, message) is expected


@pytest.mark.requirement("OBS-07")
def test_transient_fetch_stays_local_but_sink_still_records(
    captured: list[dict], tmp_path: Path
) -> None:
    """[if] TypeError Load failed reaches Sentry [then] broken, [else stop]."""
    sentry_sdk = pytest.importorskip("sentry_sdk", reason="needs the optional observability extra")

    url = "http://127.0.0.1:9448/performance"
    dropped = capture_browser_error(
        message="Load failed",
        name="TypeError",
        stack=None,
        url=url,
        user_agent="Mozilla/5.0 (Macintosh) AppleWebKit",
        context={"kind": "unhandled-rejection"},
    )
    kept = capture_browser_error(
        message="Failed to fetch dynamically imported module: /assets/x.js",
        name="TypeError",
        stack=None,
        url=url,
        user_agent="Mozilla/5.0",
        context={"kind": "unhandled-rejection"},
    )
    sentry_sdk.flush()
    assert dropped is None
    assert kept is not None
    assert len(captured) == 1
    rows = [json.loads(line) for line in (tmp_path / "sink.jsonl").read_text().splitlines()]
    assert len(rows) == 2, "the local sink must still record the dropped fetch"
    assert TRANSIENT_NETWORK_MESSAGES == {
        "Load failed",
        "Failed to fetch",
        "NetworkError when attempting to fetch resource.",
        "The user aborted a request.",
        "The operation was aborted.",
    }
