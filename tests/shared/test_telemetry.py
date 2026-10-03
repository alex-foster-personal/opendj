"""Telemetry gating, privacy scrub, and the "off means never imported" claim.

No network and no live Sentry project: the one test that proves an event
actually reaches a transport uses sentry_sdk's own in-memory transport, which
is the SDK's supported way to assert on captured envelopes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from apps.shared.telemetry import (
    ALLOWED_CONTEXT_KEYS,
    DSN_ENV,
    TELEMETRY_ENV,
    TelemetryConfigError,
    capture_browser_error,
    decide_telemetry,
    init_telemetry,
    scrub_event,
    scrub_string,
)
from tests.support.sentry_client import close_sentry_client

pytestmark = pytest.mark.no_leaked_threads

DSN = "https://public@o0.ingest.de.sentry.io/1"
SHA = "0123456789abcdef0123456789abcdef01234567"


# ----- gating -------------------------------------------------------------
def test_explicit_enable_without_dsn_raises_naming_the_variable() -> None:
    """if telemetry is enabled with no DSN then startup fails loudly."""
    with pytest.raises(TelemetryConfigError) as caught:
        decide_telemetry(
            {TELEMETRY_ENV: "1"}, build_source="payload", release=SHA
        )
    message = str(caught.value)
    assert DSN_ENV in message, "the error must name the variable to set"
    assert TELEMETRY_ENV in message


def test_explicit_enable_with_dsn_is_on_and_tagged() -> None:
    decision = decide_telemetry(
        {TELEMETRY_ENV: "on", DSN_ENV: DSN}, build_source="payload", release=SHA
    )
    assert decision.enabled is True
    assert decision.dsn == DSN
    assert decision.release == SHA, "release must be the build sha"
    assert decision.environment == "ship"


def test_explicit_disable_wins_even_with_a_dsn_present() -> None:
    decision = decide_telemetry(
        {TELEMETRY_ENV: "0", DSN_ENV: DSN}, build_source="payload", release=SHA
    )
    assert decision.enabled is False


def test_checkout_defaults_off_and_shipped_build_defaults_on() -> None:
    """OBS-04: a packaged build with a DSN reports; a checkout never does by
    default, because a DSN in a developer's .env is not consent."""
    dev = decide_telemetry({DSN_ENV: DSN}, build_source="repo", release=SHA)
    assert dev.enabled is False
    assert dev.environment == "dev"

    ship = decide_telemetry({DSN_ENV: DSN}, build_source="payload", release=SHA)
    assert ship.enabled is True
    assert ship.environment == "ship"
    assert ship.explicit is False, "a default-on build must still log, not raise"
    assert "telemetry-opt-out" in ship.reason, "the way out must be in the log line"


def test_bundled_dsn_turns_a_packaged_build_on_and_a_checkout_stays_off() -> None:
    """The dmg's telemetry.json (OBS-04). No env var at all on a tester's Mac."""
    ship = decide_telemetry({}, build_source="payload", release=SHA, bundled_dsn=DSN)
    assert ship.enabled is True
    assert ship.dsn == DSN
    assert ship.release == SHA
    assert "bundled" in ship.reason

    # A bundle can only come from a payload launcher, but the decision must
    # not trust the bundle over the build source: a checkout stays off.
    dev = decide_telemetry({}, build_source="repo", release=SHA, bundled_dsn=DSN)
    assert dev.enabled is False


def test_env_dsn_outranks_the_bundled_one() -> None:
    """A fleet operator pointing a packaged build at another key must win."""
    other = "https://public@o0.ingest.de.sentry.io/2"
    decision = decide_telemetry(
        {DSN_ENV: other}, build_source="payload", release=SHA, bundled_dsn=DSN
    )
    assert decision.dsn == other


def test_opt_out_marker_turns_a_packaged_build_off_but_not_an_explicit_enable() -> None:
    """The tester's way out, and the one thing it must not override."""
    off = decide_telemetry(
        {}, build_source="payload", release=SHA, bundled_dsn=DSN,
        opt_out="/data/telemetry-opt-out exists",
    )
    assert off.enabled is False
    assert "telemetry-opt-out" in off.reason

    # Control: an operator who asked by name is not the marker's audience.
    on = decide_telemetry(
        {TELEMETRY_ENV: "1"}, build_source="payload", release=SHA,
        bundled_dsn=DSN, opt_out="/data/telemetry-opt-out exists",
    )
    assert on.enabled is True

    # Control: OPENDJ_TELEMETRY=0 still wins over everything.
    explicit_off = decide_telemetry(
        {TELEMETRY_ENV: "0"}, build_source="payload", release=SHA, bundled_dsn=DSN
    )
    assert explicit_off.enabled is False


def test_explicit_enable_accepts_the_bundled_dsn() -> None:
    decision = decide_telemetry(
        {TELEMETRY_ENV: "1"}, build_source="payload", release=SHA, bundled_dsn=DSN
    )
    assert decision.enabled is True
    assert decision.dsn == DSN


def test_bundled_loader_reads_the_file_and_refuses_a_bad_one(tmp_path: Path) -> None:
    from apps.shared.telemetry.bundled import (
        BUNDLED_TELEMETRY_ENV,
        OPT_OUT_MARKER,
        BundledTelemetryError,
        load_bundled_telemetry,
        opt_out_reason,
    )

    assert load_bundled_telemetry({}) is None, "a checkout has no bundle"
    good = tmp_path / "telemetry.json"
    good.write_text(json.dumps({"dsn": DSN}), encoding="utf-8")
    loaded = load_bundled_telemetry({BUNDLED_TELEMETRY_ENV: str(good)})
    assert loaded is not None and loaded.dsn == DSN
    for body in ("{}", "nope", "[]"):
        bad = tmp_path / "bad.json"
        bad.write_text(body, encoding="utf-8")
        with pytest.raises(BundledTelemetryError, match=BUNDLED_TELEMETRY_ENV):
            load_bundled_telemetry({BUNDLED_TELEMETRY_ENV: str(bad)})
    with pytest.raises(BundledTelemetryError, match=BUNDLED_TELEMETRY_ENV):
        load_bundled_telemetry({BUNDLED_TELEMETRY_ENV: str(tmp_path / "missing.json")})

    assert opt_out_reason(tmp_path) is None
    (tmp_path / OPT_OUT_MARKER).touch()
    reason = opt_out_reason(tmp_path)
    assert reason is not None and OPT_OUT_MARKER in reason


def test_unidentifiable_build_is_treated_as_a_checkout() -> None:
    decision = decide_telemetry({DSN_ENV: DSN}, build_source=None, release=None)
    assert decision.enabled is False
    assert decision.environment == "dev"


def test_shipped_build_without_a_dsn_stays_off_instead_of_bricking() -> None:
    """A tester never asked for telemetry; refusing to boot would be worse."""
    decision = decide_telemetry({}, build_source="payload", release=SHA)
    assert decision.enabled is False
    assert DSN_ENV in decision.reason


def test_unrecognized_flag_value_is_refused_not_guessed() -> None:
    with pytest.raises(TelemetryConfigError):
        decide_telemetry(
            {TELEMETRY_ENV: "maybe"}, build_source="repo", release=None
        )


# ----- off means never imported ------------------------------------------
def test_disabled_never_imports_sentry_sdk() -> None:
    """if telemetry is disabled then no sentry module is initialized.

    Run in a subprocess so the assertion is about a clean interpreter rather
    than about whatever an earlier test in this session already imported.
    """
    script = textwrap.dedent(
        f"""
        import sys
        from apps.shared.telemetry import decide_telemetry, init_telemetry

        decision = decide_telemetry(
            {{"{TELEMETRY_ENV}": "0", "{DSN_ENV}": "{DSN}"}},
            build_source="payload",
            release="{SHA}",
        )
        assert init_telemetry(decision) is False
        assert "sentry_sdk" not in sys.modules, sorted(
            m for m in sys.modules if "sentry" in m
        )
        print("OK")
        """
    )
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# ----- privacy scrub ------------------------------------------------------
def test_library_paths_are_reduced_to_an_extension() -> None:
    """if a message carries a track path then artist and title do not survive."""
    scrubbed = scrub_string(
        "could not decode /Users/dev/Music/Tracks/Nina Vale - Marigold.mp3"
    )
    assert "Nina" not in scrubbed
    assert "Marigold" not in scrubbed
    assert "<path.mp3>" in scrubbed, "the extension is a real diagnostic"


def test_windows_library_paths_are_redacted_too() -> None:
    scrubbed = scrub_string(r"missing D:\Music\Artist - Title.flac")
    assert "Artist" not in scrubbed
    assert "Title" not in scrubbed


@pytest.mark.parametrize(
    "text",
    [
        "/performance",
        "/api/v1/tracks",
        "GET /api/v1/tracks?limit=50 returned 500",
        "http://127.0.0.1:8585/api/v1/build-info",
    ],
)
def test_app_routes_survive_the_path_scrub(text: str) -> None:
    """Routes are the most useful thing in an issue, and are not filesystem.

    Regression: anchoring the pattern at any leading "/" turned every route
    into "<path>", and the "p:/" inside "http://" parsed as a Windows drive.
    """
    assert scrub_string(text) == text


def test_bare_track_filenames_are_redacted_without_a_directory() -> None:
    scrubbed = scrub_string("failed on Marigold.mp3 during analysis")
    assert "Marigold" not in scrubbed
    assert "<path.mp3>" in scrubbed


def test_tokens_are_filtered() -> None:
    assert "ghp_" not in scrub_string("auth failed for ghp_abcdef1234567890")


def test_non_allowlisted_context_keys_are_redacted() -> None:
    event: dict = {
        "extra": {
            "track_title": "Nina Vale - Marigold",
            "artist": "Nina Vale",
            "deck_id": "A",
            "job_kind": "stems.separate",
        }
    }
    scrubbed = scrub_event(event)
    assert scrubbed is not None
    assert scrubbed["extra"]["track_title"] == "[redacted]"
    assert scrubbed["extra"]["artist"] == "[redacted]"
    assert scrubbed["extra"]["deck_id"] == "A", "allowlisted keys survive"
    assert scrubbed["extra"]["job_kind"] == "stems.separate"


def test_allowlist_carries_no_title_like_keys() -> None:
    """A regression guard on the allowlist itself, not on one event."""
    banned = {"title", "track", "track_title", "artist", "album", "path",
              "filename", "file_path", "query", "search"}
    assert ALLOWED_CONTEXT_KEYS & banned == set()


def test_exception_message_and_request_body_are_scrubbed() -> None:
    event: dict = {
        "exception": {
            "values": [
                {"value": "no such file: /Users/dev/Music/Secret Track.aiff"}
            ]
        },
        "request": {
            "data": {"track_title": "Marigold"},
            "cookies": {"session": "abc"},
            "headers": {"Authorization": "Bearer xyz"},
            "url": "http://127.0.0.1:8585/api/v1/tracks",
        },
        "user": {"ip_address": "10.0.0.1"},
    }
    scrubbed = scrub_event(event)
    assert scrubbed is not None
    assert "Secret" not in scrubbed["exception"]["values"][0]["value"]
    assert scrubbed["request"]["data"] == "[filtered]"
    assert scrubbed["request"]["cookies"] == "[filtered]"
    assert scrubbed["request"]["headers"] == "[filtered]"
    assert "user" not in scrubbed, "no PII block ever leaves"


def test_breadcrumb_data_is_allowlisted() -> None:
    event: dict = {
        "breadcrumbs": {
            "values": [
                {
                    "message": "loaded /Users/dev/Music/A - B.wav",
                    "data": {"deck_id": "B", "track_title": "A - B"},
                }
            ]
        }
    }
    scrubbed = scrub_event(event)
    assert scrubbed is not None
    crumb = scrubbed["breadcrumbs"]["values"][0]
    assert "A - B.wav" not in crumb["message"]
    assert crumb["data"]["deck_id"] == "B"
    assert crumb["data"]["track_title"] == "[redacted]"


def test_sdk_context_blocks_keep_their_shape() -> None:
    """Regression: redacting contexts made the SDK drop EVERY event silently.

    The client reads contexts["trace"] back while building the envelope, so a
    string there is not a privacy win, it is total telemetry loss that still
    logs "telemetry on". App-supplied blocks are still allowlisted.
    """
    event: dict = {
        "contexts": {
            "trace": {"trace_id": "abc123", "span_id": "def456"},
            "runtime": {"name": "CPython", "version": "3.11.9"},
            "now_playing": {"track_title": "Nina Vale - Marigold", "deck_id": "A"},
        }
    }
    scrubbed = scrub_event(event)
    assert scrubbed is not None
    trace = scrubbed["contexts"]["trace"]
    assert isinstance(trace, dict), "the SDK reads this back; it must stay a dict"
    assert trace["trace_id"] == "abc123"
    assert scrubbed["contexts"]["runtime"]["name"] == "CPython"
    custom = scrubbed["contexts"]["now_playing"]
    assert custom["track_title"] == "[redacted]", "app blocks are still allowlisted"
    assert custom["deck_id"] == "A"


def test_scrub_failure_drops_the_event_rather_than_leaking() -> None:
    class Hostile(dict):
        def get(self, *args, **kwargs):  # type: ignore[override]
            raise RuntimeError("boom")

    assert scrub_event(Hostile()) is None


# ----- end to end through the SDK's own transport -------------------------
def _raise_deliberate_failure() -> None:
    """The test's exception, raised from a real frame with a track path in it."""
    raise ValueError("deliberate: /Users/dev/Music/Nina Vale - Marigold.mp3")



def test_enabled_telemetry_delivers_a_scrubbed_event_to_the_transport() -> None:
    """if telemetry is enabled with a DSN then a test exception is captured.

    Uses sentry_sdk's in-memory transport: a real SDK pipeline (before_send
    included) with no network. Asserting on a live send would be a mock of a
    different kind -- a test that only passes when Sentry is reachable.
    """
    sentry_sdk = pytest.importorskip("sentry_sdk", reason="needs the optional observability extra")
    from sentry_sdk.transport import Transport

    captured: list[dict] = []

    class CapturingTransport(Transport):
        def capture_envelope(self, envelope) -> None:  # type: ignore[no-untyped-def]
            for item in envelope.items:
                payload = item.payload.json
                if payload is not None:
                    captured.append(payload)

    decision = decide_telemetry(
        {TELEMETRY_ENV: "1", DSN_ENV: DSN}, build_source="payload", release=SHA
    )
    try:
        assert init_telemetry(decision, transport=CapturingTransport()) is True
        sentry_sdk.set_extra("track_title", "Nina Vale - Marigold")
        sentry_sdk.set_extra("deck_id", "A")
        try:
            _raise_deliberate_failure()
        except ValueError as exc:
            sentry_sdk.capture_exception(exc)
        sentry_sdk.flush()
    finally:
        # Leave no global client behind for the rest of the session.
        close_sentry_client()

    assert captured, "the deliberate exception never reached the transport"
    event = captured[0]
    assert event["release"] == SHA
    assert event["environment"] == "ship"
    assert event["extra"]["deck_id"] == "A"
    assert event["extra"]["track_title"] == "[redacted]"
    assert "Marigold" not in event["exception"]["values"][0]["value"]


@pytest.fixture()
def without_sentry_sdk(monkeypatch: pytest.MonkeyPatch):
    """Make `import sentry_sdk` fail, as an unpackaged build would."""
    import builtins

    real_import = builtins.__import__

    def deny(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name.startswith("sentry_sdk"):
            raise ImportError("simulated missing observability extra")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny)


def test_explicit_enable_without_the_sdk_is_a_loud_error(
    without_sentry_sdk: None,
) -> None:
    """Asked for by name and undeliverable is a fault, not a downgrade."""
    decision = decide_telemetry(
        {TELEMETRY_ENV: "1", DSN_ENV: DSN}, build_source="payload", release=SHA
    )
    assert decision.explicit is True
    with pytest.raises(TelemetryConfigError, match="observability"):
        init_telemetry(decision)


def test_shipped_default_without_the_sdk_keeps_the_app_running(
    without_sentry_sdk: None,
) -> None:
    """A packaged build defaults ON (OBS-04) but the tester never asked by
    name, so a payload that lost its SDK logs loudly and keeps running."""
    decision = decide_telemetry(
        {DSN_ENV: DSN}, build_source="payload", release=SHA
    )
    assert decision.enabled is True
    assert decision.explicit is False
    assert init_telemetry(decision) is False


# ----- browser errors forwarded from the engine ---------------------------
def test_browser_errors_are_a_no_op_while_telemetry_is_off() -> None:
    """The common case. Every dev checkout takes this path on every error."""
    assert (
        capture_browser_error(
            message="boom",
            name="TypeError",
            stack="at x (app.js:1:1)",
            url="http://127.0.0.1:5173/performance",
            user_agent="Mozilla/5.0",
            context={"kind": "window-error"},
        )
        is None
    )


def test_browser_error_reaches_the_transport_scrubbed_and_tagged() -> None:
    """if a browser error is forwarded then it arrives with no library content.

    The browser does not talk to Sentry at all -- the CI bundle gate leaves
    about 5 KB of headroom and the smallest useful browser SDK is an order of
    magnitude larger -- so this path is the ONLY way a client error becomes an
    issue. It has to carry the diagnostic and drop the track title.
    """
    sentry_sdk = pytest.importorskip("sentry_sdk", reason="needs the optional observability extra")
    from sentry_sdk.transport import Transport

    captured: list[dict] = []

    class CapturingTransport(Transport):
        def capture_envelope(self, envelope) -> None:  # type: ignore[no-untyped-def]
            captured.extend(
                item.payload.json
                for item in envelope.items
                if item.payload.json is not None
            )

    decision = decide_telemetry(
        {TELEMETRY_ENV: "1", DSN_ENV: DSN}, build_source="payload", release=SHA
    )
    try:
        assert init_telemetry(decision, transport=CapturingTransport()) is True
        event_id = capture_browser_error(
            message="could not decode /Users/dev/Music/Nina Vale - Marigold.mp3",
            name="DOMException",
            stack="at decode (audio-engine.svelte.ts:412:9)",
            url="http://127.0.0.1:5173/performance",
            user_agent="Mozilla/5.0",
            context={
                "kind": "window-error",
                "deck_id": "A",
                "track_title": "Nina Vale - Marigold",
            },
        )
        assert event_id is not None, "the forward returned no event id"
        sentry_sdk.flush()
    finally:
        close_sentry_client()

    assert captured, "the browser error never reached the transport"
    event = captured[0]
    assert event["release"] == SHA
    assert event["environment"] == "ship"
    assert event["tags"]["origin"] == "browser", "browser events must be labeled"
    assert event["extra"]["deck_id"] == "A", "allowlisted diagnostics survive"
    assert event["extra"]["track_title"] == "[redacted]"
    body = event["message"] if isinstance(event["message"], str) else str(event["message"])
    assert "Marigold" not in body, "the track title must not reach the issue"
    assert "DOMException" in body, "the error type is the whole diagnostic"
    assert "audio-engine.svelte.ts" in body, "the browser stack must survive"
