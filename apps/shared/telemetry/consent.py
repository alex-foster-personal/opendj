"""Test-user consent: nothing is sent until the terms were accepted (OBS-05).

A packaged build ships its DSN and starts its SDK at boot (OBS-04), but a
tester has not agreed to anything by double-clicking a dmg. This module is
the gate between the two: the SDK is live, the local sink records every
event, and NOTHING leaves the machine until the consent file says
``accepted``. The frontend asks once, on first launch, with the terms in
``docs/legal/test-user-terms.md``; the answer is written to
``<data-dir>/telemetry-consent.json`` and mirrored into this process so the
next captured event sees it without a file read.

The gate applies to a build that turned itself on. An operator who set
``OPENDJ_TELEMETRY=1`` by name (a fleet or preview host) asked for
reporting and is not a test user; the gate stays open for them.

Session replay (OBS-06) is consent-only as well: the frontend loads the
Sentry loader script only after ``accepted``, from the loader URL this
module derives from the frontend DSN. Replay never runs in a build with no
frontend DSN, which is every checkout without ``SENTRY_FRONTEND_DSN``.

Stdlib only, no SDK.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast, get_args
from urllib.parse import urlparse

log = logging.getLogger(__name__)

CONSENT_FILE: str = "telemetry-consent.json"

#: Bump when docs/legal/test-user-terms.md changes materially. A stored
#: acceptance of an older version is reported as ``undecided`` so the
#: frontend asks again; nothing is sent in between.
TERMS_VERSION: str = "2026-09-21"

ConsentDecision = Literal["undecided", "accepted", "declined"]
DECISIONS: frozenset[str] = frozenset(get_args(ConsentDecision))

#: Env override for the replay session sample rate; the ship default records
#: every consented session because the test cohort is small and replay is
#: what they are being asked to provide.
REPLAY_SESSION_SAMPLE_RATE_ENV: str = "SENTRY_REPLAY_SESSION_SAMPLE_RATE"
REPLAY_SESSION_SAMPLE_RATE_DEFAULT: float = 1.0


class ConsentError(RuntimeError):
    """A consent write or read that cannot be honored."""


@dataclass(frozen=True)
class ConsentRecord:
    decision: ConsentDecision
    terms_version: str | None
    decided_at: str | None
    path: Path


class _ConsentState:
    """Process-wide mirror of the file, read by every send path."""

    __slots__ = ("granted", "required")

    def __init__(self) -> None:
        #: False for an operator-explicit enable (no gate), True otherwise.
        self.required: bool = True
        self.granted: bool = False


CONSENT = _ConsentState()


def configure_consent_gate(*, required: bool, granted: bool) -> None:
    """Called once by init_telemetry with what the boot-time file said."""
    CONSENT.required = required
    CONSENT.granted = granted


def set_consent_granted(granted: bool) -> None:
    """The route's runtime update: takes effect on the next captured event."""
    CONSENT.granted = granted


def held_for_consent() -> bool:
    """True when a send must stay local because the terms were not accepted."""
    return CONSENT.required and not CONSENT.granted


def reset_consent_for_tests() -> None:
    CONSENT.required = True
    CONSENT.granted = False


def consent_path(data_dir: Path) -> Path:
    return data_dir / CONSENT_FILE


def _undecided(path: Path) -> ConsentRecord:
    return ConsentRecord("undecided", None, None, path)


def _parse_consent(data: object, path: Path) -> ConsentRecord:
    """Interpret one decoded file body. Anything malformed is ``undecided``."""
    if not isinstance(data, dict):
        return _undecided(path)
    decision = str(data.get("decision") or "")
    version = data.get("terms_version")
    decided_at = data.get("decided_at")
    if decision not in DECISIONS or decision == "undecided":
        return _undecided(path)
    stored_version = str(version) if version is not None else None
    stored_at = str(decided_at) if decided_at is not None else None
    if decision == "accepted" and version != TERMS_VERSION:
        # Accepted OLD terms: ask again, and hold sends until they answer.
        # The stale version is reported, not hidden.
        return ConsentRecord("undecided", stored_version, stored_at, path)
    return ConsentRecord(cast(ConsentDecision, decision), stored_version, stored_at, path)


def read_consent(data_dir: Path) -> ConsentRecord:
    """The stored decision, or ``undecided`` when absent, unreadable or stale.

    Unreadable reads as undecided rather than raising: the only consequence
    is that the tester is asked again, and asking again is the safe side.
    """
    path = consent_path(data_dir)
    if not path.is_file():
        return _undecided(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("telemetry consent file %s unreadable (%s); asking again", path, exc)
        return _undecided(path)
    return _parse_consent(data, path)


def write_consent(
    data_dir: Path, decision: ConsentDecision, *, terms_version: str
) -> ConsentRecord:
    """Persist a decision. Only the CURRENT terms can be accepted."""
    if decision not in ("accepted", "declined"):
        raise ConsentError(f"decision must be accepted or declined, got {decision!r}")
    if decision == "accepted" and terms_version != TERMS_VERSION:
        raise ConsentError(
            f"terms_version {terms_version!r} is not the current {TERMS_VERSION!r}; "
            "the client must show the current terms before recording acceptance"
        )
    path = consent_path(data_dir)
    decided_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    body = {
        "decision": decision,
        "terms_version": terms_version,
        "decided_at": decided_at,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise ConsentError(f"cannot write {path}: {exc}") from exc
    return ConsentRecord(decision, terms_version, decided_at, path)


def declined_reason(data_dir: Path | None) -> str | None:
    """A boot-time opt-out reason when the stored decision is ``declined``."""
    if data_dir is None:
        return None
    record = read_consent(data_dir)
    if record.decision == "declined":
        return f"{record.path} says declined"
    return None


def replay_loader_url(frontend_dsn: str | None) -> str | None:
    """The Sentry Loader Script URL for a frontend DSN, or None without one.

    ``https://<key>@o<org>.ingest.de.sentry.io/<project>`` becomes
    ``https://js-de.sentry-cdn.com/<key>.min.js``; the CDN host follows the
    ingest region so an EU org is served from the EU CDN. The key is the
    DSN's public part and already reaches every browser that reports.
    """
    if not frontend_dsn:
        return None
    parsed = urlparse(frontend_dsn.strip())
    key = parsed.username or ""
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not key or not host:
        return None
    region = ""
    parts = host.split(".")
    # o123.ingest.de.sentry.io -> ["o123", "ingest", "de", "sentry", "io"]
    if len(parts) >= 5 and parts[1] == "ingest":
        region = parts[2]
    cdn = f"js-{region}.sentry-cdn.com" if region else "js.sentry-cdn.com"
    return f"https://{cdn}/{key}.min.js"


def replay_session_sample_rate(environ: dict[str, str] | None = None) -> float:
    import os

    env = os.environ if environ is None else environ
    raw = (env.get(REPLAY_SESSION_SAMPLE_RATE_ENV) or "").strip()
    if not raw:
        return REPLAY_SESSION_SAMPLE_RATE_DEFAULT
    try:
        rate = float(raw)
    except ValueError as exc:
        raise ConsentError(f"{REPLAY_SESSION_SAMPLE_RATE_ENV}={raw!r} is not a number") from exc
    if not 0.0 <= rate <= 1.0:
        raise ConsentError(f"{REPLAY_SESSION_SAMPLE_RATE_ENV}={raw!r} is outside [0, 1]")
    return rate


__all__ = [
    "CONSENT",
    "CONSENT_FILE",
    "DECISIONS",
    "REPLAY_SESSION_SAMPLE_RATE_DEFAULT",
    "REPLAY_SESSION_SAMPLE_RATE_ENV",
    "TERMS_VERSION",
    "ConsentDecision",
    "ConsentError",
    "ConsentRecord",
    "configure_consent_gate",
    "consent_path",
    "declined_reason",
    "held_for_consent",
    "read_consent",
    "replay_loader_url",
    "replay_session_sample_rate",
    "reset_consent_for_tests",
    "set_consent_granted",
    "write_consent",
]
