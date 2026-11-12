"""Error telemetry (Sentry) for the engine -- opt-in, DSN-gated, scrubbed.

WHY THIS EXISTS

Open DJ is going out to testers. When their engine throws, the alternative to
this module is asking a human to find a log file and paste it back, which is
exactly the attention cost the build-identity readout was built to remove.
Sentry is the runtime exception firehose; nothing else about it is in scope.
Errors by default: no profiling, no session replay, no logs, no metrics.
Request tracing is OFF unless an operator sets ``SENTRY_TRACES_SAMPLE_RATE``
(see below); it then passes the same scrubber and the same live-set gate.

NEVER WHILE A DECK IS LIVE

The reporting policy says never send while any deck is playing or audible.
:mod:`apps.shared.telemetry.live` is that rule: a browser error carries the
page's own ``any_deck_live`` flag, an engine exception asks the registered
probe (the UI mirror), and either answer of "live" keeps the event in the
local sink instead of sending it. Nothing is lost locally; Sentry just does
not see faults that fired mid-mix until the local logs are read.

THREE STATES, NEVER A SILENT ONE

``OPENDJ_TELEMETRY`` is tri-state on purpose, because "off" and "off because
the DSN was missing" are different faults and only one of them is acceptable.

- unset      -> the build decides. A PACKAGED build (a dmg) ships its DSN
                in the payload (OBS-04) and is ON: installing the app is the
                consent, and the tester can turn it off by creating
                ``telemetry-opt-out`` in the engine data directory. A repo
                CHECKOUT is OFF; a DSN in a developer's .env is not consent,
                and fleet test builds opt in with OPENDJ_TELEMETRY=1.
- 1/true/on  -> ON, and a missing DSN is a HARD ERROR at startup. Somebody
                asked for telemetry by name; starting without it would mean
                the errors they were waiting on never arrive and nothing ever
                says so.
- 0/false/off-> OFF, and ``sentry_sdk`` is never imported. Not "imported and
                idle" -- never imported, which is the only version of off
                that a test can prove.

A shipped build whose DSN is absent is the one case that does NOT raise: the
tester did not ask for telemetry, and bricking their app to report that the
developer forgot a build variable would be a worse failure than the one it
reports. It logs loudly and stays off. That asymmetry is deliberate. (The
payload build now fails without a DSN, so this is a damaged install, not a
build that was ever shipped this way.)

WHAT LEAVES THE MACHINE

The scrub itself is :mod:`apps.shared.telemetry.scrub`, kept pure and
separate so the privacy policy can be read without the SDK around it.

This is the ONLY scrubber in the app. The browser does not report to Sentry
itself (see :func:`capture_browser_error`), so every event that leaves the
machine -- engine exception or forwarded browser error -- passes through
here. There is no second implementation to keep in agreement.

The library is the user's private music collection, and a track title is the
single most identifying thing this app touches. So the payload is an
ALLOWLIST, not a denylist: ``extra``, ``tags`` and ``contexts`` keep only
:data:`ALLOWED_CONTEXT_KEYS` and every other key becomes ``[redacted]``.
Free text is an allowlist as well (OBS-6393): a forwarded log or a breadcrumb
carries its logging template and numeric arguments, an exception carries
its type and stack, and the rendered text stays in the local logs.
Filesystem paths are rewritten to their extension alone, in exception
messages and breadcrumbs as well as context. Frame-local variables are OFF
(``include_local_variables=False``) -- that switch is what stops a local
named ``track`` carrying an artist and title into an issue. See
:func:`scrub_event` and ``docs/telemetry.md`` for the full inventory.

Requirements:

- ✔︎ ✅ 🎯 Telemetry explicitly enabled with no DSN raises at startup, naming
  the variable to set. -> :func:`decide_telemetry`
- ✔︎ ✅ 🎯 Telemetry disabled leaves ``sentry_sdk`` unimported.
  -> :func:`init_telemetry`
- ✔︎ ✅ 🎯 A repo checkout defaults OFF; a payload build with a bundled DSN
  defaults ON and honors the opt-out marker. -> :func:`decide_telemetry`
- ✔︎ ✅ 🎯 Track titles and library paths never reach an event payload.
  -> :func:`scrub_event`
- ✔︎ ✅ 🎯 Free text forwards templates and types, never rendered content
  (OBS-6393). -> :func:`forwardable_log_message`, :func:`_before_breadcrumb`
- ✔︎ ✅ 🎯 Events carry the build sha as the release and dev/ship as the
  environment. -> :func:`decide_telemetry`

Acceptance tests:

- [if] OPENDJ_TELEMETRY=1 and SENTRY_DSN is empty [then] decide_telemetry
  raises TelemetryConfigError naming SENTRY_DSN, [else ⛔️].
- [if] OPENDJ_TELEMETRY=0 [then] init_telemetry returns disabled and
  sys.modules has no "sentry_sdk", [else ⛔️].
- [if] an event carries extra={"track_title": "..."} [then] the scrubbed
  event's extra value is "[redacted]", [else ⛔️].
- [if] an exception message contains /Users/x/Music/Artist - Title.mp3
  [then] the scrubbed message contains neither "Artist" nor "Title", [else ⛔️].
- [if] a log line, a breadcrumb or an exception message names an artist, a
  title or a track file [then] none of them is anywhere in the serialized
  event, with the path filter bypassed too, [else ⛔️].

This module is a LEAF on purpose: apps.engine_core imports apps.webui, so
anything both of them use has to live somewhere neither owns, or the
import graph closes a cycle. That is why the build-identity lookup that
feeds decide_telemetry lives in the engine entry point rather than here.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import urlsplit

from apps.shared.telemetry.consent import (
    configure_consent_gate,
    held_for_consent,
)
from apps.shared.telemetry.decision import (
    DSN_ENV,
    ENVIRONMENT_ENV,
    ENVIRONMENTS,
    TELEMETRY_ENV,
    TRACES_SAMPLE_RATE_ENV,
    TelemetryConfigError,
    TelemetryDecision,
    decide_telemetry,
)
from apps.shared.telemetry.live import (
    MIRROR_LIVE_MAX_AGE_S,
    MIRROR_RECEIVED_MONOTONIC_KEY,
    mirror_age_s,
    mirror_decks_live,
    mirror_transport_live,
    set_live_transport_probe,
    transport_is_live,
)
from apps.shared.telemetry.scrub import (
    ALLOWED_CONTEXT_KEYS,
    BREADCRUMB_TEXT_WITHHELD,
    BUG_REPORT_ALLOWED_KEYS,
    BUG_REPORT_TEXT_MAX_BYTES,
    EXCEPTION_VALUE_WITHHELD,
    FILTERED,
    LOG_ARG_WITHHELD,
    LOG_TEXT_WITHHELD,
    REDACTED,
    SAFE_EXCEPTION_TYPES,
    SDK_CONTEXT_BLOCKS,
    ForwardableText,
    assert_bug_report_text_safe,
    forwardable_log_message,
    scrub_bug_report_mapping,
    scrub_event,
    scrub_string,
    truncate_bug_report_text,
)

log = logging.getLogger(__name__)


class _LiveGateCounter:
    """Events the live-set gate kept local this process. Read by tests."""

    __slots__ = ("suppressed",)

    def __init__(self) -> None:
        self.suppressed: int = 0


LIVE_GATE = _LiveGateCounter()


class _LastDecision:
    """The decision init_telemetry ran with, for routes that report it."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value: TelemetryDecision | None = None


LAST_DECISION = _LastDecision()


# ----- init ---------------------------------------------------------------
def init_telemetry(
    decision: TelemetryDecision,
    *,
    transport: Any | None = None,
    consent_granted: bool = False,
) -> bool:
    """Initialize the SDK for an enabled decision. Returns whether it ran.

    ``consent_granted`` is what the consent file said at boot (OBS-05). It
    matters only for a build that turned itself on: an explicit enable is an
    operator asking by name and is never held for consent.

    ``sentry_sdk`` is imported HERE and nowhere else in the module, so a
    disabled decision leaves it absent from ``sys.modules`` and the "off
    means off" acceptance test can prove it by inspection.

    ``transport`` is a test seam, and the only one. The acceptance test for
    "an enabled build actually delivers an event" has to run the real init --
    these exact options, this before_send, this release -- or it is testing a
    hand-built client that resembles production rather than production. The
    SDK supports an injected transport for precisely this, so the test can
    assert on a captured envelope without a live send. Production passes
    None and gets the SDK's own HTTP transport.
    """
    LAST_DECISION.value = decision
    if not decision.enabled:
        log.info("telemetry off (%s)", decision.reason)
        return False
    configure_consent_gate(required=not decision.explicit, granted=consent_granted)

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError as exc:
        detail = (
            "telemetry is enabled but sentry-sdk is not installed. Install it "
            "with `uv sync --extra observability`, or set "
            f"{TELEMETRY_ENV}=0 to run without telemetry."
        )
        if decision.explicit:
            raise TelemetryConfigError(detail) from exc
        # A SHIPPED build that defaulted itself on and was packaged without
        # the optional extra must not refuse to boot: the user never asked
        # for telemetry, and taking their app away to report a packaging
        # mistake is a far worse outcome than losing the diagnostics. Loud in
        # the log, silent to the user, still running.
        log.error("%s Continuing without telemetry.", detail)
        return False

    sentry_sdk.init(
        dsn=decision.dsn,
        environment=decision.environment,
        release=decision.release,
        transport=transport,
        # Errors only unless SENTRY_TRACES_SAMPLE_RATE opts this process into
        # request tracing. Profiling stays a deliberate zero.
        traces_sample_rate=decision.traces_sample_rate,
        before_send_transaction=_before_send_transaction,
        profiles_sample_rate=0.0,
        # The privacy switches. include_local_variables is the important one:
        # frame locals are where a track title would otherwise ride along.
        send_default_pii=False,
        include_local_variables=False,
        max_breadcrumbs=25,
        before_send=_before_send,
        before_breadcrumb=_before_breadcrumb,
        integrations=[
            # Logs are breadcrumbs only. The default (event_level=ERROR) sent
            # every log.error as a second, unscrubbed event: 92 of the org's
            # 219 events on Mon 14 Sep 2026. Errors reach Sentry through the
            # explicit captures and warning_log's forward, never the logger.
            LoggingIntegration(level=logging.INFO, event_level=None, sentry_logs_level=None),
            StarletteIntegration(failed_request_status_codes=set()),
            FastApiIntegration(failed_request_status_codes=set()),
        ],
    )
    log.info(
        "telemetry on (environment=%s, release=%s, traces_sample_rate=%s, consent=%s, %s)",
        decision.environment,
        decision.release or "unstamped",
        decision.traces_sample_rate,
        "not required" if decision.explicit else ("granted" if consent_granted else "held"),
        decision.reason,
    )
    return True


def _keep_local_while_live(what: str) -> bool:
    """True, counted and logged once per process, when a deck is live right now."""
    if not transport_is_live():
        return False
    if LIVE_GATE.suppressed == 0:
        log.info(
            "live-set gate: a deck is playing, so this %s and any that follow "
            "stay in the local sink until transport stops",
            what,
        )
    LIVE_GATE.suppressed += 1
    return True


def reset_live_gate_for_tests() -> None:
    LIVE_GATE.suppressed = 0
    set_live_transport_probe(None)


def _before_send(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Stamp error id / host / sha, hold while live, spend the budget, then scrub.

    Order matters. The sink row is written FIRST so the local record is
    complete whatever happens next. SQLITE_BUSY / ``database is locked``
    (OBS-5380) is dropped after that row, before the live-set gate and budget,
    because STATE-14 already published it as HTTP 503. The live-set gate runs
    BEFORE the budget so a mid-mix fault does not spend quota on an event
    that is not sent.
    """
    from apps.shared.telemetry.budget import SENTRY_BUDGET
    from apps.shared.telemetry.sink import enrich_sentry_event, event_error_id

    try:
        enrich_sentry_event(event, hint)
    except Exception:
        log.warning("error-sink enrich failed", exc_info=True)
    from apps.shared.telemetry.sqlite_busy import (
        exception_from_sentry_hint,
        is_local_only_sqlite_busy,
    )

    if is_local_only_sqlite_busy(
        exc=exception_from_sentry_hint(hint),
        event=event,
    ):
        return None
    # A forwarded browser error already passed the gate in capture_browser_error
    # on the page's own flag, which outranks the engine's mirror read; asking
    # the probe again here would let a stale "playing" mirror overrule it.
    if held_for_consent():
        return None
    from_browser = (event.get("tags") or {}).get("origin") == "browser"
    if not from_browser and _keep_local_while_live("error"):
        return None
    if not SENTRY_BUDGET.admit(event_error_id(event) or "eid-unidentified"):
        return None
    return cast("dict[str, Any] | None", scrub_event(event, hint))


def _before_breadcrumb(crumb: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """A breadcrumb carries the log TEMPLATE, never the rendered line (OBS-6393).

    Up to 25 breadcrumbs ride on every error event, and the logging
    integration fills each one with the fully rendered message. The record
    itself arrives in the hint, so the template is still recoverable here and
    nowhere later. A breadcrumb that did not come from a log record has free
    text nobody can vouch for, so its message becomes a placeholder; its
    ``data`` is allowlisted by the scrub like any other.
    """
    record = (hint or {}).get("log_record")
    if isinstance(record, logging.LogRecord):
        crumb["message"] = forwardable_log_message(record)
    elif crumb.get("message") is not None:
        crumb["message"] = BREADCRUMB_TEXT_WITHHELD
    return crumb


def _before_send_transaction(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Tracing only: same live-set gate, same scrub. No sink row, no budget.

    A transaction is not an error, so it mints no error id and writes no
    JSONL; and the SDK's own sample rate already bounds how many there are.
    What it must not do is add work to a live set or carry a library path in
    a span, hence the gate and the scrub.
    """
    if held_for_consent() or _keep_local_while_live("transaction"):
        return None
    return cast("dict[str, Any] | None", scrub_event(event, hint))


# ----- browser errors -----------------------------------------------------
#: Payload fields worth carrying as Sentry context. The client error contract
#: (apps/webui/server/routes/client_errors.py) has more fields than Sentry
#: needs, and everything not named here stays in the local daily log only.
BROWSER_CONTEXT_FIELDS: tuple[str, ...] = (
    "kind",
    "client_event_id",
    "secure_context",
    "audio_worklet_available",
    "any_deck_live",
)


def _browser_error_held(context: Mapping[str, Any]) -> bool:
    """The live-set gate for a browser error: the page's flag outranks the mirror."""
    any_deck_live = context.get("any_deck_live")
    if any_deck_live is True:
        return True
    return any_deck_live is None and transport_is_live()


#: One stack frame: V8 ``at fn (url:line:col)`` / ``at url:line:col``, or
#: Firefox and Safari ``fn@url:line:col``. A stack's header (``Error: <message>``)
#: and any line V8 repeats the message on match neither, so they are dropped.
_BROWSER_FRAME = re.compile(
    r"^\s*at (?:\S.* \()?(?P<v8>\S+?):(?P<v8l>\d+):(?P<v8c>\d+)\)?\s*$"
    r"|^\S*@(?P<url>\S+?):(?P<line>\d+):(?P<col>\d+)\s*$"
)
#: The app's own code, as Vite serves it in dev and SvelteKit builds it. Any
#: other URL path (a route, a blob) is page-chosen text and does not travel.
_BROWSER_CODE_PATH = re.compile(r"^/(?:src|_app|node_modules|@vite|@id)/[\w./@~+-]*\.(?:m?js|cjs|ts|svelte)$")
BROWSER_FRAME_WITHHELD: str = "    at [frame withheld]"
#: JavaScript's and the DOM's own error types; any other ``Error.name`` is
#: set by page code and may carry anything.
BROWSER_ERROR_NAMES: frozenset[str] = frozenset(
    {
        "Error",
        "TypeError",
        "RangeError",
        "ReferenceError",
        "SyntaxError",
        "EvalError",
        "URIError",
        "AggregateError",
        "DOMException",
        "AbortError",
        "NotAllowedError",
        "NotFoundError",
        "NotSupportedError",
        "InvalidStateError",
        "SecurityError",
        "QuotaExceededError",
        "NetworkError",
        "TimeoutError",
        "EncodingError",
        "DataError",
    }
)
BROWSER_ERROR_NAME_WITHHELD: str = "[error name withheld]"


def _browser_frame(line: str) -> str | None:
    match = _BROWSER_FRAME.match(line)
    if match is None:
        return None
    url, row, col = (match["v8"], match["v8l"], match["v8c"]) if match["v8"] else match.group("url", "line", "col")
    path = urlsplit(url).path
    # The function name is page-chosen text too; source maps recover it from the position.
    return f"    at {path}:{row}:{col}" if _BROWSER_CODE_PATH.match(path) else BROWSER_FRAME_WITHHELD


def browser_stack_frames(stack: str | None) -> str:
    """A browser stack rebuilt from its frames: code path and position only."""
    frames = (_browser_frame(line) for line in (stack or "").splitlines())
    return "\n".join(frame for frame in frames if frame is not None)


def capture_browser_error(
    *,
    message: str,
    name: str | None,
    stack: str | None,
    url: str,
    user_agent: str,
    context: Mapping[str, Any],
) -> str | None:
    """Forward one already-reported browser error to Sentry.

    WHY THE ENGINE AND NOT THE BROWSER

    Bundling the Sentry browser SDK is not available to this app: the CI
    bundle gate allows 250 KB gzipped across all chunks and main already sits
    at ~253 KB of it, leaving under 3 KB, while the smallest useful
    errors-only browser client is an order of magnitude larger. Lazy-loading
    does not help, because the gate sums every emitted chunk.

    That constraint turns out to be a better design anyway. Every client
    error already arrives here through the browser's own durable queue
    (`reportClientError` -> POST /api/v1/client-errors), which survives an
    engine that is temporarily down, so nothing is lost by reporting from
    this side. One scrubber then governs every event the app sends, browser
    and engine alike, instead of two implementations that have to be kept
    in agreement.

    Returns the Sentry event id, or None when telemetry is off (the common
    case, and not a failure), when the error is a perf-event console mirror,
    a Vite dev-tooling console line, an expected WebMIDI permission denial,
    a transient fetch/abort TypeError (OBS-07), an expected library
    play-refusal (OBS-5422), or a STATE-14 ``STATE_STORE_BUSY`` RbApiError
    (OBS-5454; token match only),
    when consent is not granted, when a deck is live, or when the quota
    budget refused it. Never raises: reporting an error must not become one.

    THE LIVE-SET GATE. ``context["any_deck_live"]`` is the page's own read
    of its transport at the moment the error fired, and it is authoritative
    when it is a bool: True keeps the event local, False sends it whatever
    the engine's mirror says. Only a client that did not send the field
    (None) falls back to the engine-side probe. The local sink row above
    is written either way.
    """
    from apps.shared.telemetry.budget import is_local_only_browser_error
    from apps.shared.telemetry.sink import (
        capture_error_event,
        client_error_message,
        client_source_site,
    )

    kind = str(context.get("kind") or "client")
    safe_name = name if name in BROWSER_ERROR_NAMES else BROWSER_ERROR_NAME_WITHHELD
    record = capture_error_event(
        message=client_error_message(safe_name, EXCEPTION_VALUE_WITHHELD),
        source_site=client_source_site(kind, url),
        kind="client",
        # Raw text for the local JSONL only; identity and the forwarded event keep the safe form.
        local_message=client_error_message(name, message),
    )
    if is_local_only_browser_error(kind, name, message):
        return None
    if _client() is None or held_for_consent():
        return None
    if _browser_error_held(context):
        LIVE_GATE.suppressed += 1
        return None
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.set_tag("origin", "browser")
            scope.set_tag("error_id", record.error_id)
            scope.set_tag("host", record.host)
            scope.set_tag("build_sha", record.build_sha)
            scope.set_tag("kind", "client")
            scope.set_tag("source_site", record.source_site)
            scope.fingerprint = [record.error_id]
            scope.set_context(
                "browser_error",
                {
                    "user_agent": user_agent,
                    "url": url,
                    **{key: context[key] for key in BROWSER_CONTEXT_FIELDS if key in context},
                },
            )
            for key, value in context.items():
                scope.set_extra(key, value)
            # A browser stack is a string here, not a Python traceback, so it
            # travels as the message body. Sentry groups on it the same way.
            # The browser's message is withheld like a Python exception's.
            body = client_error_message(safe_name, EXCEPTION_VALUE_WITHHELD)
            frames = browser_stack_frames(stack)
            if frames:
                body = f"{body}\n{frames}"
            return sentry_sdk.capture_message(ForwardableText(body), level="error")
    except Exception:
        log.warning("forwarding a browser error to Sentry failed", exc_info=True)
        return None


def _client() -> Any | None:
    """The active Sentry client, or None when telemetry never initialized.

    Imports sentry_sdk ONLY when init_telemetry already did, so the "off
    means never imported" guarantee survives this call path too.
    """
    if "sentry_sdk" not in sys.modules:
        return None
    import sentry_sdk

    client = sentry_sdk.get_client()
    # is_active() alone is true for the SDK's own no-DSN placeholder client
    # (what `sentry_sdk.init(dsn=None)` leaves behind), which can send nothing.
    if client is None or not client.is_active() or not client.dsn:
        return None
    return client


__all__ = [
    "ALLOWED_CONTEXT_KEYS",
    "BREADCRUMB_TEXT_WITHHELD",
    "BROWSER_CONTEXT_FIELDS",
    "BUG_REPORT_ALLOWED_KEYS",
    "BUG_REPORT_TEXT_MAX_BYTES",
    "DSN_ENV",
    "ENVIRONMENTS",
    "ENVIRONMENT_ENV",
    "EXCEPTION_VALUE_WITHHELD",
    "FILTERED",
    "LAST_DECISION",
    "LIVE_GATE",
    "LOG_ARG_WITHHELD",
    "LOG_TEXT_WITHHELD",
    "MIRROR_LIVE_MAX_AGE_S",
    "MIRROR_RECEIVED_MONOTONIC_KEY",
    "REDACTED",
    "SAFE_EXCEPTION_TYPES",
    "SDK_CONTEXT_BLOCKS",
    "TELEMETRY_ENV",
    "TRACES_SAMPLE_RATE_ENV",
    "ForwardableText",
    "TelemetryConfigError",
    "TelemetryDecision",
    "assert_bug_report_text_safe",
    "browser_stack_frames",
    "capture_browser_error",
    "decide_telemetry",
    "forwardable_log_message",
    "init_telemetry",
    "mirror_age_s",
    "mirror_decks_live",
    "mirror_transport_live",
    "reset_live_gate_for_tests",
    "scrub_bug_report_mapping",
    "scrub_event",
    "scrub_string",
    "set_live_transport_probe",
    "transport_is_live",
    "truncate_bug_report_text",
]
