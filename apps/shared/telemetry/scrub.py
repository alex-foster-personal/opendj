"""The privacy scrub: what an event may carry off this machine.

Split out of the telemetry package's gating half because it is PURE -- no
SDK, no environment, no I/O -- so it can be read and tested as policy on its
own. Everything here answers one question: given an event, which parts of it
are safe to send?

This is the ONLY scrubber in the app. The browser does not report to Sentry
itself, so every event that leaves the machine -- engine exception or
forwarded browser error -- passes through here.

The library is the user's private music collection, and a track title is the
single most identifying thing this app touches. So the payload is an
ALLOWLIST, not a denylist: a key nobody approved becomes ``[redacted]``,
which means a new field is invisible in Sentry until somebody adds it ON
PURPOSE. A missing diagnostic costs one PR; a leaked track title cannot be
taken back.

``docs/telemetry.md`` carries the full inventory in prose.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, MutableMapping
from typing import Any

log = logging.getLogger(__name__)

#: Context keys that may travel verbatim. Everything else is redacted, so a
#: new key is invisible in Sentry until it is added here ON PURPOSE. That is
#: the intended failure direction: a missing diagnostic costs one PR, a
#: leaked track title cannot be taken back.
ALLOWED_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        # request / routing
        "route",
        "url",
        "origin",
        "method",
        "status",
        "http_status",
        "error_code",
        "source",
        "kind",
        # engine identity
        "engine_version",
        "contract_rev",
        "boot_id",
        "build_source",
        "lane_label",
        # jobs
        "job_id",
        "job_kind",
        "attempt",
        # playback surfaces (ids and numbers only, never titles)
        "deck_id",
        "adapter",
        "sample_rate",
        "channels",
        "duration_ms",
        "count",
        # browser errors forwarded by the engine
        "client_event_id",
        "user_agent",
        "fallback_message",
        "secure_context",
        "audio_worklet_available",
        "any_deck_live",
        # host
        "platform",
        "python_version",
        # OBS-01 identity on the one sink
        "error_id",
        "host",
        "build_sha",
        "source_site",
    }
)

#: Context blocks the SDK builds and then READS BACK. ``contexts["trace"]``
#: in particular must still be a mapping after before_send: the client pulls
#: the trace id out of it while assembling the envelope, and a string there
#: makes the SDK drop the event without a word. Found the hard way -- the
#: first version of this scrub allowlisted context keys like any other
#: section, and every single event vanished between before_send and the
#: transport while the logs cheerfully said telemetry was on.
#:
#: These blocks are machine facts (OS, runtime, trace ids), not library
#: content, so their STRUCTURE is preserved and only their string leaves are
#: scrubbed. Any other block is app-supplied and gets the full allowlist.
SDK_CONTEXT_BLOCKS: frozenset[str] = frozenset(
    {
        "app",
        "browser",
        "cloud_resource",
        "culture",
        "device",
        "gpu",
        "missing_instrumentation",
        "os",
        "profile",
        "replay",
        "response",
        "runtime",
        "trace",
    }
)

REDACTED: str = "[redacted]"
FILTERED: str = "[filtered]"

#: Filesystem roots. Anchoring to REAL roots rather than to any leading "/"
#: is what keeps app routes readable: "/performance" and "/api/v1/tracks" are
#: among the most useful things in an issue, and an earlier version of this
#: pattern redacted both into "<path>" while claiming to protect privacy.
#:
#: The \b before the Windows drive letter matters: without it the "p:/"
#: inside "http://" parses as a drive and every URL gets mangled.
_FS_ROOT = (
    r"(?:\b[A-Za-z]:[\\/]|~[\\/]|"
    r"/(?:Users|home|Volumes|mnt|media|private|var|tmp|opt|srv|root|Library"
    r"|System|Applications)[\\/])"
)

#: Extensions that identify library content wherever they appear.
_AUDIO_EXTENSIONS = "mp3|flac|wav|aiff|aif|m4a|ogg|aac|opus|alac|wma|aax"

#: Paths and track filenames, reduced to their extension. The extension
#: survives because "it broke on a .flac" is a real diagnostic and carries
#: nothing about which .flac.
#:
#: Three branches, in order: a rooted path running up to an extension with
#: SPACES ALLOWED (track filenames contain them, and a space-terminated
#: pattern would redact ".../Music/Fred" and leave the artist and title in
#: the message); a rooted directory with no extension; and a bare filename
#: ending in an audio extension, for a track name with no directory in front.
#:
_PATH_RE = re.compile(
    rf"{_FS_ROOT}[^\r\n\"'<>|?*]*?\.[A-Za-z0-9]{{1,8}}\b"
    rf"|{_FS_ROOT}[^\s\"'<>|?*\r\n]*"
    rf"|[^\s\"'<>|?*\r\n/\\]+\.(?:{_AUDIO_EXTENSIONS})\b",
    re.IGNORECASE,
)

_EXTENSION_RE = re.compile(r"(\.[A-Za-z0-9]{1,8})$")

#: Bearer / API-key shaped strings. Kept from the earlier adapter because a
#: token in an error message is still a token.
_TOKENISH_RE = re.compile(
    r"(?:bearer\s+)[a-z0-9._\-+=/]{8,}|"
    r"sk-[a-z0-9]{10,}|"
    r"ghp_[a-z0-9]{10,}|"
    r"xox[baprs]-[a-z0-9-]{10,}|"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|passwd|"
    r"authorization)\s*[:=]\s*['\"]?[^\s'\"]{8,}",
    re.IGNORECASE,
)


def _redact_paths(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        found = _EXTENSION_RE.search(match.group(0))
        return f"<path{found.group(1)}>" if found else "<path>"

    return _PATH_RE.sub(replace, value)


def scrub_string(value: str) -> str:
    """Token-shaped and path-shaped substrings out of any free text."""
    return _redact_paths(_TOKENISH_RE.sub(FILTERED, value))


def _allowlist(value: Any) -> Any:
    """Keep allowlisted keys, redact the rest, scrub what survives."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name.lower() not in ALLOWED_CONTEXT_KEYS:
                out[name] = REDACTED
            else:
                out[name] = _allowlist(item)
        return out
    if isinstance(value, list):
        return [_allowlist(item) for item in value]
    if isinstance(value, str):
        return scrub_string(value)
    return value


def _scrub_values(value: Any) -> Any:
    """Scrub string leaves, keep every key and the shape around them."""
    if isinstance(value, dict):
        return {str(k): _scrub_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_values(item) for item in value]
    if isinstance(value, str):
        return scrub_string(value)
    return value


def _scrub_contexts(contexts: Any) -> Any:
    """SDK blocks keep their shape; app-supplied blocks get the allowlist."""
    if not isinstance(contexts, dict):
        return _allowlist(contexts)
    out: dict[str, Any] = {}
    for name, block in contexts.items():
        if str(name).lower() in SDK_CONTEXT_BLOCKS:
            out[str(name)] = _scrub_values(block)
        else:
            out[str(name)] = _allowlist(block)
    return out


def _scrub_sections(event: MutableMapping[str, Any]) -> None:
    """The app-supplied blocks: strict allowlist, except SDK context shape."""
    for section in ("extra", "tags"):
        if event.get(section) is not None:
            event[section] = _allowlist(event[section])
    if event.get("contexts") is not None:
        event["contexts"] = _scrub_contexts(event["contexts"])


def _scrub_exception(event: MutableMapping[str, Any]) -> None:
    """An exception message is free text written by whoever raised it, which
    makes it the most likely place for a filename to appear."""
    exception = event.get("exception")
    if not isinstance(exception, dict):
        return
    for entry in exception.get("values") or []:
        if isinstance(entry, dict) and isinstance(entry.get("value"), str):
            entry["value"] = scrub_string(entry["value"])


def _scrub_message(event: MutableMapping[str, Any]) -> None:
    message = event.get("message")
    if isinstance(message, str):
        event["message"] = scrub_string(message)
    elif isinstance(message, dict) and isinstance(message.get("formatted"), str):
        message["formatted"] = scrub_string(message["formatted"])


def _scrub_logentry(event: MutableMapping[str, Any]) -> None:
    """A logged message travels as template + params + formatted, not `message`.

    Until Mon 14 Sep 2026 this block was never scrubbed, and the top issue in
    the live org carried a raw /Users/... path in its title this way.
    """
    logentry = event.get("logentry")
    if not isinstance(logentry, dict):
        return
    for key in ("message", "formatted"):
        if isinstance(logentry.get(key), str):
            logentry[key] = scrub_string(logentry[key])
    params = logentry.get("params")
    if isinstance(params, list):
        logentry["params"] = [
            scrub_string(value) if isinstance(value, str) else value for value in params
        ]


def _scrub_request(event: MutableMapping[str, Any]) -> None:
    request = event.get("request")
    if not isinstance(request, dict):
        return
    # Bodies, cookies and headers are dropped wholesale rather than filtered:
    # none of them carry a diagnostic this app needs.
    for key in ("data", "cookies", "headers", "env"):
        if key in request:
            request[key] = FILTERED
    for key in ("url", "query_string"):
        if isinstance(request.get(key), str):
            request[key] = scrub_string(request[key])


def _scrub_spans(event: MutableMapping[str, Any]) -> None:
    """Transactions only: the route name and every span's free text and data.

    A span description is where an integration writes a URL or a statement,
    and span ``data`` is an open dict, so both get the same treatment as a
    breadcrumb: text through the path/token filter, data through the
    allowlist. Absent on an error event, and a no-op there.
    """
    if isinstance(event.get("transaction"), str):
        event["transaction"] = scrub_string(event["transaction"])
    spans = event.get("spans")
    if not isinstance(spans, list):
        return
    for span in spans:
        if not isinstance(span, dict):
            continue
        if isinstance(span.get("description"), str):
            span["description"] = scrub_string(span["description"])
        if span.get("data") is not None:
            span["data"] = _allowlist(span["data"])
        if span.get("tags") is not None:
            span["tags"] = _allowlist(span["tags"])


def _scrub_breadcrumbs(event: MutableMapping[str, Any]) -> None:
    breadcrumbs = event.get("breadcrumbs")
    values = breadcrumbs.get("values") if isinstance(breadcrumbs, dict) else breadcrumbs
    for crumb in values or []:
        if not isinstance(crumb, dict):
            continue
        if isinstance(crumb.get("message"), str):
            crumb["message"] = scrub_string(crumb["message"])
        if crumb.get("data") is not None:
            crumb["data"] = _allowlist(crumb["data"])


def scrub_event(
    event: MutableMapping[str, Any],
    hint: Mapping[str, Any] | None = None,  # noqa: ARG001 - before_send signature
) -> MutableMapping[str, Any] | None:
    """Strip library content and secrets. Drops the event if scrubbing fails.

    Fail-closed on purpose: an event that could not be scrubbed is an event
    whose contents are unknown, and the safe thing to do with unknown
    contents is not send them.
    """
    try:
        _scrub_sections(event)
        _scrub_exception(event)
        _scrub_message(event)
        _scrub_logentry(event)
        _scrub_request(event)
        _scrub_breadcrumbs(event)
        _scrub_spans(event)
        # send_default_pii=False already suppresses these; belt and braces
        # because a future integration could set them directly.
        event.pop("user", None)
        event.pop("server_name", None)
    except Exception:
        log.warning("telemetry scrub failed; dropping the event", exc_info=True)
        return None
    else:
        return event
