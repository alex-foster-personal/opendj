"""Stable error ids: hash of source site + message class.

An error id is greppable (``eid-`` plus 12 hex chars) and stable across
hosts, timestamps, and volatile tokens in the message (job numbers, uuids,
shas). Two records that share a raising site and a message class share an id.

Used by the one error sink (OBS-01 / issue #2320). Stdlib only, so CI and
the headless-dmg hook can import it without sentry-sdk.
"""

from __future__ import annotations

import hashlib
import re

ERROR_ID_PREFIX: str = "eid-"
ERROR_ID_HEX_LEN: int = 12

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_HEX_RE = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_NUM_RE = re.compile(r"\b\d+\b")
_WS_RE = re.compile(r"\s+")

#: One placeholder so a uuid, a sha, and an integer collapse to the same class.
_TOKEN: str = "<tok>"


def classify_message(message: str) -> str:
    """Strip volatile tokens so 'job 17 failed' and 'job 99 failed' match."""
    text = message.strip().lower()
    text = _UUID_RE.sub(_TOKEN, text)
    text = _HEX_RE.sub(_TOKEN, text)
    text = _NUM_RE.sub(_TOKEN, text)
    return _WS_RE.sub(" ", text).strip()


def stable_error_id(*, source_site: str, message: str) -> str:
    """Return ``eid-`` plus 12 hex chars of sha256(site + class)."""
    site = source_site.strip().lower()
    klass = classify_message(message)
    digest = hashlib.sha256(f"{site}\n{klass}".encode()).hexdigest()
    return f"{ERROR_ID_PREFIX}{digest[:ERROR_ID_HEX_LEN]}"
