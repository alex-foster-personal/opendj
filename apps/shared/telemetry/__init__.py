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

This module is a LEAF on purpose: apps.engine_core imports apps.webui, so
anything both of them use has to live somewhere neither owns, or the
import graph closes a cycle. That is why the build-identity lookup that
feeds decide_telemetry lives in the engine entry point rather than here.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

from apps.shared.telemetry.consent import (
    configure_consent_gate,
    held_for_consent,
)
from apps.shared.telemetry.live import (
    MIRROR_LIVE_MAX_AGE_S,
    mirror_transport_live,
    set_live_transport_probe,
    transport_is_live,
)
from apps.shared.telemetry.scrub import (
    ALLOWED_CONTEXT_KEYS,
    FILTERED,
    REDACTED,
    SDK_CONTEXT_BLOCKS,
    scrub_event,
    scrub_string,
)

log = logging.getLogger(__name__)

#: Fraction of requests that become a Sentry transaction. Unset or ``0``
#: means errors only, which is the default and the only mode a packaged
#: build has run in. Any value in (0, 1] turns request tracing on for this
#: process; the transactions pass :func:`_before_send_transaction`, which is
#: the same scrub and the same live-set gate as an error. Read under the
#: name the SDK documents so an operator who knows Sentry can find it.
TRACES_SAMPLE_RATE_ENV: str = "SENTRY_TRACES_SAMPLE_RATE"


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

#: Tri-state switch. Unset means "let the build decide" (see module docstring).
TELEMETRY_ENV: str = "OPENDJ_TELEMETRY"

#: Read under the name the Sentry SDK itself documents, so an operator who
#: knows Sentry does not have to learn a private spelling. The value lives in
#: the gitignored root .env, and in Doppler general/dev_personal as
#: OPENDJ_SENTRY_DSN_BACKEND.
DSN_ENV: str = "SENTRY_DSN"

#: Sentry's own variable name too. afmac's opendj-preview sets it to
#: "preview"; until Mon 14 Sep 2026 init passed environment= explicitly and
#: silently overrode it to "dev", so preview traffic was mislabeled.
ENVIRONMENT_ENV: str = "SENTRY_ENVIRONMENT"

TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})
FALSEY: frozenset[str] = frozenset({"0", "false", "no", "off"})

Environment = Literal["dev", "preview", "fleet", "ship"]
ENVIRONMENTS: frozenset[str] = frozenset(get_args(Environment))

class TelemetryConfigError(RuntimeError):
    """Telemetry was asked for by name and cannot be delivered."""


@dataclass(frozen=True)
class TelemetryDecision:
    """What was decided, and the reason, so a log line can state both."""

    enabled: bool
    environment: Environment
    reason: str
    dsn: str | None = None
    release: str | None = None
    #: True only when an operator set OPENDJ_TELEMETRY on by hand. It decides
    #: how hard a downstream fault lands: somebody who asked for telemetry by
    #: name gets an exception, a shipped default gets a loud log. Branching on
    #: this rather than on ``reason`` keeps that rule out of a message string.
    explicit: bool = False
    #: 0.0 is errors only. See :data:`TRACES_SAMPLE_RATE_ENV`.
    traces_sample_rate: float = 0.0

    def __post_init__(self) -> None:
        if self.enabled and not self.dsn:
            raise ValueError(
                "an enabled TelemetryDecision must carry a DSN; construct it "
                "through decide_telemetry, which enforces that invariant"
            )
        if not 0.0 <= self.traces_sample_rate <= 1.0:
            raise ValueError(
                f"traces_sample_rate must be within [0, 1], got {self.traces_sample_rate!r}"
            )


# ----- decision -----------------------------------------------------------
def _flag(raw: str | None) -> bool | None:
    """True / False when the operator was explicit, None when they were not."""
    if raw is None:
        return None
    value = raw.strip().lower()
    if value == "":
        return None
    if value in TRUTHY:
        return True
    if value in FALSEY:
        return False
    raise TelemetryConfigError(
        f"{TELEMETRY_ENV}={raw!r} is not a recognized flag. Use one of "
        f"{sorted(TRUTHY)} to enable or {sorted(FALSEY)} to disable, or "
        "unset it to let the build decide."
    )


def _environment(environ: Mapping[str, str], build_source: str | None) -> Environment:
    """SENTRY_ENVIRONMENT when set (and known), else ship for a payload, dev otherwise."""
    raw = (environ.get(ENVIRONMENT_ENV) or "").strip()
    if not raw:
        return "ship" if build_source == "payload" else "dev"
    if raw not in ENVIRONMENTS:
        raise TelemetryConfigError(
            f"{ENVIRONMENT_ENV}={raw!r} is not one of {sorted(ENVIRONMENTS)}. "
            "Unset it to let the build decide."
        )
    return cast(Environment, raw)


def _traces_sample_rate(environ: Mapping[str, str]) -> float:
    """SENTRY_TRACES_SAMPLE_RATE parsed and range-checked; unset or blank is 0."""
    raw = (environ.get(TRACES_SAMPLE_RATE_ENV) or "").strip()
    if not raw:
        return 0.0
    try:
        rate = float(raw)
    except ValueError:
        rate = float("nan")
    if not 0.0 <= rate <= 1.0:
        raise TelemetryConfigError(
            f"{TRACES_SAMPLE_RATE_ENV}={raw!r} is not a number in [0, 1]. Use 0 "
            "(or unset it) for errors only, or a fraction of requests to trace."
        )
    return rate


def decide_telemetry(
    environ: Mapping[str, str],
    *,
    build_source: str | None,
    release: str | None,
    bundled_dsn: str | None = None,
    opt_out: str | None = None,
) -> TelemetryDecision:
    """Resolve the three states into one decision. Never guesses.

    ``build_source`` is the ``source`` field of the resolved build identity
    ("payload" for an installed build, "repo" for a checkout). None means the
    build could not describe itself, which is treated as a checkout: a build
    that cannot say what it is has no business reporting under a release.

    ``bundled_dsn`` is the DSN a packaged build carries in its payload
    (:mod:`apps.shared.telemetry.bundled`); ``opt_out`` is the reason a
    tester's opt-out marker gives when it exists. Both are plain values so
    this function reads no files: the entry point resolves them.
    """
    environment = _environment(environ, build_source)
    env_dsn = (environ.get(DSN_ENV) or "").strip()
    dsn = env_dsn or (bundled_dsn or "").strip()
    explicit = _flag(environ.get(TELEMETRY_ENV))
    # Parsed before the on/off branch so a typo fails loud on every boot,
    # not only on the one where telemetry happens to be on.
    traces_sample_rate = _traces_sample_rate(environ)

    if explicit is False:
        return TelemetryDecision(
            enabled=False,
            environment=environment,
            reason=f"{TELEMETRY_ENV} is set to off",
        )

    if explicit is True:
        if not dsn:
            raise TelemetryConfigError(
                f"{TELEMETRY_ENV} is on but {DSN_ENV} is empty or unset and no "
                "DSN is bundled, so no error would ever reach Sentry and "
                f"nothing downstream would report that. Set {DSN_ENV} to the "
                f"Open DJ backend DSN, or unset {TELEMETRY_ENV} to let the "
                "build decide."
            )
        return TelemetryDecision(
            enabled=True,
            environment=environment,
            reason=f"{TELEMETRY_ENV} is set to on",
            dsn=dsn,
            release=release,
            explicit=True,
            traces_sample_rate=traces_sample_rate,
        )

    # Unset, and the tester said no with the marker file. Checked after the
    # explicit flag on purpose: an operator who sets OPENDJ_TELEMETRY=1 on a
    # fleet build is asking by name and the marker is not theirs to honor;
    # an unset flag on a tester's Mac is exactly whom the marker exists for.
    if opt_out:
        return TelemetryDecision(
            enabled=False,
            environment=environment,
            reason=f"telemetry opted out: {opt_out}",
        )

    # Unset on a PACKAGED build with a DSN to hand: ON (OBS-04). Installing
    # the dmg is the consent; the marker above is the way out.
    if build_source == "payload" and dsn:
        origin = "bundled DSN" if not env_dsn else f"{DSN_ENV} in the environment"
        return TelemetryDecision(
            enabled=True,
            environment=environment,
            reason=(
                f"packaged build ships telemetry ({origin}); create "
                f"'telemetry-opt-out' in the data directory or set "
                f"{TELEMETRY_ENV}=0 to turn it off"
            ),
            dsn=dsn,
            release=release,
            traces_sample_rate=traces_sample_rate,
        )

    # Unset on a checkout (or a packaged build with no DSN): OFF. A DSN in a
    # developer's .env is not consent; fleet test builds set OPENDJ_TELEMETRY=1.
    if build_source == "payload":
        reason = (
            f"packaged build carries no DSN: neither {DSN_ENV} nor a bundled "
            "telemetry.json; this install was damaged or built before OBS-04"
        )
    else:
        reason = (
            "telemetry defaults to off in a checkout; a "
            f"{DSN_ENV} in the environment is not consent. set "
            f"{TELEMETRY_ENV}=1 to opt in"
        )
    return TelemetryDecision(enabled=False, environment=environment, reason=reason)


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


def _before_send(
    event: dict[str, Any], hint: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Stamp error id / host / sha, hold while live, spend the budget, then scrub.

    Order matters. The sink row is written FIRST so the local record is
    complete whatever happens next; the live-set gate runs BEFORE the budget
    so a mid-mix fault does not spend quota on an event that is not sent.
    """
    from apps.shared.telemetry.budget import SENTRY_BUDGET
    from apps.shared.telemetry.sink import enrich_sentry_event, event_error_id

    try:
        enrich_sentry_event(event, hint)
    except Exception:
        log.warning("error-sink enrich failed", exc_info=True)
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


def _before_send_transaction(
    event: dict[str, Any], hint: dict[str, Any] | None = None
) -> dict[str, Any] | None:
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
    when a deck is live, or when the quota budget refused it. Never raises:
    reporting an error must not become one.

    THE LIVE-SET GATE. ``context["any_deck_live"]`` is the page's own read
    of its transport at the moment the error fired, and it is authoritative
    when it is a bool: True keeps the event local, False sends it whatever
    the engine's mirror says. Only a client that did not send the field
    (None) falls back to the engine-side probe. The local sink row above
    is written either way.
    """
    from apps.shared.telemetry.budget import is_dev_tooling_console, is_perf_console_mirror
    from apps.shared.telemetry.sink import (
        capture_error_event,
        client_error_message,
        client_source_site,
    )

    kind = str(context.get("kind") or "client")
    record = capture_error_event(
        message=client_error_message(name, message),
        source_site=client_source_site(kind, url),
        kind="client",
    )
    if is_perf_console_mirror(kind, message) or is_dev_tooling_console(kind, message):
        return None
    client = _client()
    if client is None or held_for_consent():
        return None
    any_deck_live = context.get("any_deck_live")
    if any_deck_live is True or (any_deck_live is None and transport_is_live()):
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
                    **{
                        key: context[key]
                        for key in BROWSER_CONTEXT_FIELDS
                        if key in context
                    },
                },
            )
            for key, value in context.items():
                scope.set_extra(key, value)
            # A browser stack is a string here, not a Python traceback, so it
            # travels as the message body. Sentry groups on it the same way.
            body = client_error_message(name, message)
            if stack:
                body = f"{body}\n{stack}"
            return sentry_sdk.capture_message(body, level="error")
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
    "BROWSER_CONTEXT_FIELDS",
    "DSN_ENV",
    "ENVIRONMENTS",
    "ENVIRONMENT_ENV",
    "FILTERED",
    "LAST_DECISION",
    "LIVE_GATE",
    "MIRROR_LIVE_MAX_AGE_S",
    "REDACTED",
    "SDK_CONTEXT_BLOCKS",
    "TELEMETRY_ENV",
    "TRACES_SAMPLE_RATE_ENV",
    "TelemetryConfigError",
    "TelemetryDecision",
    "capture_browser_error",
    "decide_telemetry",
    "init_telemetry",
    "mirror_transport_live",
    "reset_live_gate_for_tests",
    "scrub_event",
    "scrub_string",
    "set_live_transport_probe",
    "transport_is_live",
]
