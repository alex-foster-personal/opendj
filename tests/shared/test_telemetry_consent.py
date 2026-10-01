"""OBS-05 / OBS-06: nothing is sent until the test user accepts the terms.

[if] a packaged build has a live SDK and no accepted consent [then] a captured
exception reaches the local sink and never the transport, [else stop].
[if] the consent route records accepted [then] the next exception is sent
without a restart, [else stop].
[if] an operator enabled telemetry by name [then] no consent is required, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared.telemetry import (
    DSN_ENV,
    TELEMETRY_ENV,
    capture_browser_error,
    decide_telemetry,
    init_telemetry,
)
from apps.shared.telemetry.consent import (
    TERMS_VERSION,
    ConsentError,
    consent_path,
    declined_reason,
    held_for_consent,
    read_consent,
    replay_loader_url,
    replay_session_sample_rate,
    reset_consent_for_tests,
    set_consent_granted,
    write_consent,
)
from tests.support.sentry_client import close_sentry_client

pytestmark = [pytest.mark.requirement("OBS-05"), pytest.mark.no_leaked_threads]

DSN = "https://public@o0.ingest.de.sentry.io/1"
SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture()
def consent_reset():
    reset_consent_for_tests()
    yield
    reset_consent_for_tests()


def _capturing_transport(captured: list[dict]):  # type: ignore[no-untyped-def]
    from sentry_sdk.transport import Transport

    class CapturingTransport(Transport):
        def capture_envelope(self, envelope) -> None:  # type: ignore[no-untyped-def]
            captured.extend(
                item.payload.json for item in envelope.items if item.payload.json is not None
            )

    return CapturingTransport()


def _raise() -> None:
    raise ValueError("deliberate: consent test")


# ----- the file --------------------------------------------------------------
def test_absent_file_is_undecided(tmp_path: Path) -> None:
    record = read_consent(tmp_path)
    assert record.decision == "undecided"
    assert record.path == consent_path(tmp_path)


def test_accept_and_decline_round_trip(tmp_path: Path) -> None:
    accepted = write_consent(tmp_path, "accepted", terms_version=TERMS_VERSION)
    assert accepted.decision == "accepted"
    assert read_consent(tmp_path).decision == "accepted"
    assert declined_reason(tmp_path) is None
    body = json.loads(consent_path(tmp_path).read_text(encoding="utf-8"))
    assert body["terms_version"] == TERMS_VERSION
    assert body["decided_at"].endswith("Z")

    write_consent(tmp_path, "declined", terms_version=TERMS_VERSION)
    assert read_consent(tmp_path).decision == "declined"
    reason = declined_reason(tmp_path)
    assert reason is not None and "declined" in reason


def test_acceptance_of_old_terms_reads_as_undecided(tmp_path: Path) -> None:
    """Bumping TERMS_VERSION must ask again, not carry consent forward."""
    consent_path(tmp_path).write_text(
        json.dumps({"decision": "accepted", "terms_version": "1999-01-01"}), encoding="utf-8"
    )
    record = read_consent(tmp_path)
    assert record.decision == "undecided"
    assert record.terms_version == "1999-01-01", "the stale version is reported, not hidden"


def test_accepting_stale_terms_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConsentError, match="current"):
        write_consent(tmp_path, "accepted", terms_version="1999-01-01")
    assert not consent_path(tmp_path).exists()


@pytest.mark.parametrize("body", ["nope", "[]", "{}", '{"decision": "maybe"}'])
def test_unreadable_file_asks_again(tmp_path: Path, body: str) -> None:
    consent_path(tmp_path).write_text(body, encoding="utf-8")
    assert read_consent(tmp_path).decision == "undecided"


# ----- the loader URL -----------------------------------------------------------
@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (
            "https://public@o0.ingest.de.sentry.io/4511790810136656",
            "https://js-de.sentry-cdn.com/public.min.js",
        ),
        ("https://public@o0.ingest.us.sentry.io/2", "https://js-us.sentry-cdn.com/public.min.js"),
        ("https://public@o0.ingest.sentry.io/2", "https://js.sentry-cdn.com/public.min.js"),
        (None, None),
        ("", None),
        ("not a dsn", None),
        ("http://public@o0.ingest.de.sentry.io/2", None),
    ],
)
def test_replay_loader_url_follows_the_ingest_region(
    dsn: str | None, expected: str | None
) -> None:
    assert replay_loader_url(dsn) == expected


def test_replay_session_sample_rate_defaults_to_full_and_refuses_junk() -> None:
    assert replay_session_sample_rate({}) == 1.0
    assert replay_session_sample_rate({"SENTRY_REPLAY_SESSION_SAMPLE_RATE": "0.25"}) == 0.25
    with pytest.raises(ConsentError):
        replay_session_sample_rate({"SENTRY_REPLAY_SESSION_SAMPLE_RATE": "2"})
    with pytest.raises(ConsentError):
        replay_session_sample_rate({"SENTRY_REPLAY_SESSION_SAMPLE_RATE": "lots"})


# ----- the hold ------------------------------------------------------------------
def test_default_on_build_holds_every_send_until_accepted(consent_reset: None) -> None:
    """The whole point: a live SDK, an undecided tester, nothing leaves.
    Control in the same test: after acceptance the very next event is sent."""
    import sentry_sdk

    captured: list[dict] = []
    decision = decide_telemetry({}, build_source="payload", release=SHA, bundled_dsn=DSN)
    assert decision.enabled is True and decision.explicit is False
    try:
        assert init_telemetry(
            decision, transport=_capturing_transport(captured), consent_granted=False
        )
        assert held_for_consent() is True
        try:
            _raise()
        except ValueError as exc:
            sentry_sdk.capture_exception(exc)
        assert (
            capture_browser_error(
                message="boom", name="Error", stack=None, url="http://x/performance",
                user_agent="ua", context={"kind": "ui-error", "any_deck_live": False},
            )
            is None
        )
        sentry_sdk.flush()
        assert captured == [], "an event left before the terms were accepted"

        set_consent_granted(True)
        assert held_for_consent() is False
        try:
            _raise()
        except ValueError as exc:
            sentry_sdk.capture_exception(exc)
        sentry_sdk.flush()
    finally:
        close_sentry_client()
    assert len(captured) == 1, "acceptance must take effect without a restart"


def test_consent_granted_at_boot_sends_from_the_first_event(consent_reset: None) -> None:
    import sentry_sdk

    captured: list[dict] = []
    decision = decide_telemetry({}, build_source="payload", release=SHA, bundled_dsn=DSN)
    try:
        assert init_telemetry(
            decision, transport=_capturing_transport(captured), consent_granted=True
        )
        try:
            _raise()
        except ValueError as exc:
            sentry_sdk.capture_exception(exc)
        sentry_sdk.flush()
    finally:
        close_sentry_client()
    assert len(captured) == 1


def test_explicit_enable_is_not_held_for_consent(consent_reset: None) -> None:
    """A fleet host that set OPENDJ_TELEMETRY=1 asked by name."""
    import sentry_sdk

    captured: list[dict] = []
    decision = decide_telemetry(
        {TELEMETRY_ENV: "1", DSN_ENV: DSN}, build_source="repo", release=SHA
    )
    try:
        assert init_telemetry(
            decision, transport=_capturing_transport(captured), consent_granted=False
        )
        assert held_for_consent() is False
        try:
            _raise()
        except ValueError as exc:
            sentry_sdk.capture_exception(exc)
        sentry_sdk.flush()
    finally:
        close_sentry_client()
    assert len(captured) == 1
