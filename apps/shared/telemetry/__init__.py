"""Error telemetry (Sentry) for the engine -- opt-in, DSN-gated, scrubbed.

WHY THIS EXISTS

Open DJ is going out to testers. When their engine throws, the alternative to
this module is asking a human to find a log file and paste it back, which is
exactly the attention cost the build-identity readout was built to remove.
Sentry is the runtime exception firehose; nothing else about it is in scope.
Errors only: no traces, no profiling, no session replay, no performance data.

THREE STATES, NEVER A SILENT ONE

``OPENDJ_TELEMETRY`` is tri-state on purpose, because "off" and "off because
the DSN was missing" are different faults and only one of them is acceptable.

- unset      -> OFF. A payload build (installed, shipped to a tester) and a
                repo checkout both stay off until a consent UX exists. Fleet
                test builds set OPENDJ_TELEMETRY=1. A DSN in the environment
                is not consent.
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
reports. It logs loudly and stays off. That asymmetry is deliberate.

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
- ✔︎ ✅ 🎯 A repo checkout and a payload build both default OFF.
  -> :func:`decide_telemetry`
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

from apps.shared.telemetry.scrub import (
    ALLOWED_CONTEXT_KEYS,
    FILTERED,
    REDACTED,
    SDK_CONTEXT_BLOCKS,
    scrub_event,
    scrub_string,
)

log = logging.getLogger(__name__)

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

    def __post_init__(self) -> None:
        if self.enabled and not self.dsn:
            raise ValueError(
                "an enabled TelemetryDecision must carry a DSN; construct it "
                "through decide_telemetry, which enforces that invariant"
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


def decide_telemetry(
    environ: Mapping[str, str],
    *,
    build_source: str | None,
    release: str | None,
) -> TelemetryDecision:
    """Resolve the three states into one decision. Never guesses.

    ``build_source`` is the ``source`` field of the resolved build identity
    ("payload" for an installed build, "repo" for a checkout). None means the
    build could not describe itself, which is treated as a checkout: a build
    that cannot say what it is has no business reporting under a release.
    """
    environment = _environment(environ, build_source)
    dsn = (environ.get(DSN_ENV) or "").strip()
    explicit = _flag(environ.get(TELEMETRY_ENV))

    if explicit is False:
        return TelemetryDecision(
            enabled=False,
            environment=environment,
            reason=f"{TELEMETRY_ENV} is set to off",
        )

    if explicit is True:
        if not dsn:
            raise TelemetryConfigError(
                f"{TELEMETRY_ENV} is on but {DSN_ENV} is empty or unset, so "
                "no error would ever reach Sentry and nothing downstream "
                f"would report that. Set {DSN_ENV} to the Open DJ backend "
                f"DSN, or unset {TELEMETRY_ENV} to let the build decide."
            )
        return TelemetryDecision(
            enabled=True,
            environment=environment,
            reason=f"{TELEMETRY_ENV} is set to on",
            dsn=dsn,
            release=release,
            explicit=True,
        )

    # Unset: default OFF everywhere until a consent UX exists (OBS-01).
    # Fleet test builds set OPENDJ_TELEMETRY=1. A DSN is not consent.
    return TelemetryDecision(
        enabled=False,
        environment=environment,
        reason=(
            "telemetry defaults to off; a "
            f"{DSN_ENV} in the environment is not consent. set "
            f"{TELEMETRY_ENV}=1 to opt in"
        ),
    )


# ----- init ---------------------------------------------------------------
def init_telemetry(
    decision: TelemetryDecision, *, transport: Any | None = None
) -> bool:
    """Initialize the SDK for an enabled decision. Returns whether it ran.

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
    if not decision.enabled:
        log.info("telemetry off (%s)", decision.reason)
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
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
        # Errors only. Every one of these is a deliberate zero.
        traces_sample_rate=0.0,
        profiles_sample_rate=0.0,
        # The privacy switches. include_local_variables is the important one:
        # frame locals are where a track title would otherwise ride along.
        send_default_pii=False,
        include_local_variables=False,
        max_breadcrumbs=25,
        before_send=_before_send,
        integrations=[
            StarletteIntegration(failed_request_status_codes=set()),
            FastApiIntegration(failed_request_status_codes=set()),
        ],
    )
    log.info(
        "telemetry on (environment=%s, release=%s, %s)",
        decision.environment,
        decision.release or "unstamped",
        decision.reason,
    )
    return True


def _before_send(
    event: dict[str, Any], hint: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Stamp error id / host / sha, spend the quota budget, then run the privacy scrub."""
    from apps.shared.telemetry.budget import SENTRY_BUDGET
    from apps.shared.telemetry.sink import enrich_sentry_event, event_error_id

    try:
        enrich_sentry_event(event, hint)
    except Exception:
        log.warning("error-sink enrich failed", exc_info=True)
    if not SENTRY_BUDGET.admit(event_error_id(event) or "eid-unidentified"):
        return None
    return scrub_event(event, hint)


# ----- browser errors -----------------------------------------------------
#: Payload fields worth carrying as Sentry context. The client error contract
#: (apps/webui/server/routes/client_errors.py) has more fields than Sentry
#: needs, and everything not named here stays in the local daily log only.
BROWSER_CONTEXT_FIELDS: tuple[str, ...] = (
    "kind",
    "client_event_id",
    "secure_context",
    "audio_worklet_available",
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
    or when the quota budget refused it. Never raises: reporting an error
    must not become one.
    """
    from apps.shared.telemetry.budget import is_perf_console_mirror
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
    if is_perf_console_mirror(kind, message):
        return None
    client = _client()
    if client is None:
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
    return client if client is not None and client.is_active() else None


__all__ = [
    "ALLOWED_CONTEXT_KEYS",
    "BROWSER_CONTEXT_FIELDS",
    "DSN_ENV",
    "ENVIRONMENTS",
    "ENVIRONMENT_ENV",
    "FILTERED",
    "REDACTED",
    "SDK_CONTEXT_BLOCKS",
    "TELEMETRY_ENV",
    "TelemetryConfigError",
    "TelemetryDecision",
    "capture_browser_error",
    "decide_telemetry",
    "init_telemetry",
    "scrub_event",
    "scrub_string",
]
