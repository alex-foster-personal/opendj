"""The telemetry decision: on, off, or refused, and why (OBS-01, OBS-04).

Pure: reads a mapping, returns a :class:`TelemetryDecision`, raises
:class:`TelemetryConfigError`. No SDK, no files, no environment access of its
own. ``apps.shared.telemetry.__init__`` re-exports everything here; this
module exists so the decision can be read and tested as policy on its own,
the same reason ``scrub.py`` is separate from the SDK calls.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast, get_args

#: Fraction of requests that become a Sentry transaction. Unset or ``0``
#: means errors only, which is the default and the only mode a packaged
#: build has run in. Any value in (0, 1] turns request tracing on for this
#: process; the transactions pass :func:`_before_send_transaction`, which is
#: the same scrub and the same live-set gate as an error. Read under the
#: name the SDK documents so an operator who knows Sentry can find it.
TRACES_SAMPLE_RATE_ENV: str = "SENTRY_TRACES_SAMPLE_RATE"


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
