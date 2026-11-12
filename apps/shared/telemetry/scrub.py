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

import inspect
import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import PurePosixPath, PureWindowsPath
from types import CodeType
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


# ----- free text: an allowlist, not a filter --------------------------------
#: Free text is where library content travels: a rendered log line, a
#: breadcrumb, an exception message. ``scrub_string`` cannot recognize an
#: artist or a track title, only the shapes it was taught (rooted paths,
#: tokens, a bare audio filename without spaces), so on its own it let
#: ``Artist - Some Title.mp3: decode failed`` through with the artist and most
#: of the title intact. These channels therefore forward only what is known
#: to be code: a logging TEMPLATE proven to be a source literal, numeric
#: arguments, the level, the logger name, the exception type. Everything else
#: becomes one of the placeholders below. ``scrub_string`` still runs
#: afterwards as a second layer.
LOG_TEXT_WITHHELD: str = "[log text withheld: template not proven a source literal]"
LOG_ARG_WITHHELD: str = "<arg>"
EXCEPTION_VALUE_WITHHELD: str = "[exception message withheld]"
BREADCRUMB_TEXT_WITHHELD: str = "[breadcrumb text withheld]"
EXCEPTION_TYPE_WITHHELD: str = "[exception type withheld]"

#: Exception type names whose MESSAGE may travel (still through
#: ``scrub_string``). Empty on purpose: a type is added here only when every
#: place that raises it is known to put no library content in the message.
SAFE_EXCEPTION_TYPES: frozenset[str] = frozenset()


class _WithheldArg:
    """Stands in for a non-numeric logging argument when a template renders."""

    def __str__(self) -> str:
        return LOG_ARG_WITHHELD

    def __repr__(self) -> str:
        return LOG_ARG_WITHHELD

    def __format__(self, format_spec: str) -> str:
        return LOG_ARG_WITHHELD


_WITHHELD_ARG = _WithheldArg()


class _WithheldMapping(dict[str, Any]):
    """Mapping arguments for ``%(key)s``; as a whole (``%s``) its keys stay home."""

    def __str__(self) -> str:
        return LOG_ARG_WITHHELD

    def __repr__(self) -> str:
        return LOG_ARG_WITHHELD


def _numeric_or_withheld(value: Any) -> Any:
    """Exact int, float and bool pass; every other type is withheld.

    ``type(...) in`` rather than ``isinstance`` on purpose: a subclass can
    carry its own ``__str__`` (an enum member prints its name), and the rule
    here is that only a plain number is known to be content-free.
    """
    return value if type(value) in (int, float, bool) else _WITHHELD_ARG


def _template_with_numeric_args(template: str, args: Any) -> str:
    """Render a proven template, keeping numbers and withholding everything else.

    Only called after ``record.getMessage()`` succeeded, so the template and
    its arguments are known to match in count and keys.
    """
    if not args:
        return template  # what getMessage does: no arguments, no % applied
    if isinstance(args, Mapping):
        safe: Any = _WithheldMapping({str(key): _numeric_or_withheld(item) for key, item in args.items()})
    else:
        safe = tuple(_numeric_or_withheld(item) for item in args)
    try:
        return str(template % safe)
    except TypeError:
        # The one failure left once getMessage has passed: a numeric directive
        # (%d, %x, %.1f) that accepted the real argument, an IntEnum say, met
        # the withheld stand-in. The bare template is a proven literal: code.
        return template


#: How many frames the provenance walks look at. Breadcrumbs run this on hot logging paths;
#: unbounded, a 200-frame stack cost about 32 us a record against 6 us at 10 frames (box,
#: Thu 15 Oct 2026). A caller deeper than this gets no proof, so its text is withheld (fail closed).
_MAX_PROVENANCE_FRAMES = 64


def _calling_code(record: logging.LogRecord) -> CodeType | None:
    """The code object that made this logging call, if it is on this stack.

    A handler runs synchronously inside the logging call, so the caller's
    frame is an ancestor of this one, suspended at ``record.lineno``. A record
    handled later or elsewhere (a queue, another thread) has no such frame
    here, and gets None.
    """
    frame = inspect.currentframe()
    for _ in range(_MAX_PROVENANCE_FRAMES):
        if frame is None:
            break
        if frame.f_code.co_filename == record.pathname and frame.f_lineno == record.lineno:
            return frame.f_code
        frame = frame.f_back
    return None


def _is_constant_of(code: CodeType | None, value: object) -> bool:
    """True only when ``value`` IS one of ``code``'s constants.

    A literal is loaded from ``co_consts``, so it is that very object. An
    f-string, a concatenation or a pre-formatted message is built at run time
    and is never one of them, whatever it contains.

    Known gap: CPython interns 1-character and empty strings, so a runtime
    ``str`` of that length can be the same object as a constant and pass. Such
    text is too short to carry a title or path, so this is accepted.
    """
    return isinstance(value, str) and code is not None and any(const is value for const in code.co_consts)


def _template_is_source_literal(record: logging.LogRecord) -> bool:
    """True only when ``record.msg`` IS one of the calling code's constants.

    No calling frame found means no proof, and no proof means withheld.
    """
    return _is_constant_of(_calling_code(record), record.msg)


def _is_source_literal_on_stack(value: object) -> bool:
    """True only when ``value`` IS a constant of a frame on this stack.

    ``before_send`` runs synchronously inside ``capture_message``, so the
    code that passed a literal is an ancestor of this frame.
    """
    frame = inspect.currentframe()
    for _ in range(_MAX_PROVENANCE_FRAMES):
        if frame is None:
            break
        if _is_constant_of(frame.f_code, value):
            return True
        frame = frame.f_back
    return False


class ForwardableText(str):
    """Text assembled only from what an OBS-6393 allowlist already forwards.

    The str subclass is the proof: it survives the SDK's serializer into
    ``before_send``, and a plain runtime string never carries it.
    """


def _withheld_log_message(record: logging.LogRecord) -> str:
    """Level, logger, file basename and line around the fixed placeholder."""
    return (
        f"{record.levelname} {record.name}: {LOG_TEXT_WITHHELD} "
        f"({record.filename}:{record.lineno} in {record.funcName})"
    )


def forwardable_log_message(record: logging.LogRecord) -> ForwardableText:
    """What one log record may say off this machine. Never its rendered text.

    A template proven to be a string literal of the calling code travels,
    with its numeric arguments. Anything else -- an f-string or concatenated
    template, with or without arguments, or a record handled away from its
    call -- travels as level, logger name and code location around a fixed
    placeholder. An attached exception contributes its type name only.
    """
    # Rendered for its failure, never its text: a malformed call (too few
    # arguments, a missing key) raises here exactly as a standard handler's
    # format() makes it raise without telemetry installed.
    record.getMessage()
    exc_type = record.exc_info[0] if record.exc_info else None
    suffix = f" [{exc_type.__name__}]" if exc_type is not None else ""
    if _template_is_source_literal(record):
        return ForwardableText(_template_with_numeric_args(str(record.msg), record.args) + suffix)
    return ForwardableText(_withheld_log_message(record) + suffix)


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


def forwardable_exception_type(type_name: Any, module: Any) -> tuple[str, str | None]:
    """The ``(type, module)`` an exception entry may carry off the machine.

    A class built at run time (``type(title, (Exception,), {})``) puts any
    string in ``__name__`` and ``__module__``, so the name travels only when it
    resolves to a module-level exception class of that name in a loaded module
    (``builtins`` when the SDK sends no module). Anything else, a dynamic class
    or a nested one, becomes :data:`EXCEPTION_TYPE_WITHHELD` with no module.
    """
    if isinstance(type_name, str) and (module is None or isinstance(module, str)):
        owner = sys.modules.get(module or "builtins")
        candidate = getattr(owner, type_name, None) if owner is not None else None
        if isinstance(candidate, type) and issubclass(candidate, BaseException):
            return type_name, module
    return EXCEPTION_TYPE_WITHHELD, None


def _scrub_exception(event: MutableMapping[str, Any]) -> None:
    """An exception message is free text written by whoever raised it, which
    makes it the most likely place for a track filename or title to appear.

    So the message does not travel: the stack does, the type and the module
    do when :func:`forwardable_exception_type` proves them static names, and
    the value becomes a fixed placeholder unless the type is named in
    :data:`SAFE_EXCEPTION_TYPES`.
    """
    for entry in _values(event.get("exception")):
        if not isinstance(entry, dict):
            continue
        for key in [k for k in entry if k not in _EXCEPTION_FIELDS]:
            del entry[key]
        if isinstance(entry.get("mechanism"), dict):
            entry["mechanism"] = {k: v for k, v in entry["mechanism"].items() if k in _MECHANISM_FIELDS}
        _scrub_stacks_of(entry)
        if "type" in entry or "module" in entry:
            entry["type"], module = forwardable_exception_type(entry.get("type"), entry.get("module"))
            if module is None:
                entry.pop("module", None)
        value = entry.get("value")
        if entry.get("type") in SAFE_EXCEPTION_TYPES and isinstance(value, str):
            entry["value"] = scrub_string(value)
        elif value is not None:
            entry["value"] = EXCEPTION_VALUE_WITHHELD


#: What an exception entry carries off the machine; anything else (a
#: mechanism description, its data) is free text and is dropped.
_EXCEPTION_FIELDS: frozenset[str] = frozenset(
    {"type", "module", "value", "stacktrace", "raw_stacktrace", "mechanism", "thread_id"}
)
_MECHANISM_FIELDS: frozenset[str] = frozenset(
    {"type", "handled", "synthetic", "exception_id", "parent_id", "is_exception_group", "source"}
)


def _values(block: Any) -> list[Any]:
    """The entries of a ``{"values": [...]}`` interface, or of its bare-list form."""
    values = block.get("values") if isinstance(block, dict) else block
    return values if isinstance(values, list) else []


#: Every key under which the protocol nests a stack.
_STACKTRACE_KEYS: tuple[str, ...] = ("stacktrace", "raw_stacktrace")


def _scrub_stacks_of(holder: Any) -> None:
    """Scrub each stack an exception entry, a thread or the event carries."""
    if isinstance(holder, Mapping):
        for key in _STACKTRACE_KEYS:
            _scrub_frames(holder.get(key))


def _scrub_stacktraces(event: MutableMapping[str, Any]) -> None:
    """Frames outside an exception: the event's own stack and every thread's."""
    _scrub_stacks_of(event)
    for thread in _values(event.get("threads")):
        _scrub_stacks_of(thread)


def _file_basename_if_absolute(filename: str) -> str:
    """An absolute ``filename`` (a script outside any package) keeps its basename."""
    if PurePosixPath(filename).is_absolute() or PureWindowsPath(filename).is_absolute():
        return PureWindowsPath(filename).name
    return filename


#: The only frame fields that travel. Source context (``pre_context``,
#: ``context_line``, ``post_context``) is source text that can spell library
#: content; ``abs_path`` names the home directory; ``vars`` are frame locals.
_FRAME_FIELDS: frozenset[str] = frozenset({"filename", "module", "function", "lineno", "colno", "in_app", "platform"})


def _scrub_frames(stacktrace: Any) -> None:
    """Each frame is rebuilt from :data:`_FRAME_FIELDS`, nothing else.

    An absolute ``filename`` (a script outside any package) keeps its basename.
    """
    if not isinstance(stacktrace, dict):
        return
    stacktrace["frames"] = [
        {key: value for key, value in frame.items() if key in _FRAME_FIELDS}
        for frame in stacktrace.get("frames") or []
        if isinstance(frame, dict)
    ]
    for frame in stacktrace["frames"]:
        if isinstance(frame.get("filename"), str):
            frame["filename"] = scrub_string(_file_basename_if_absolute(frame["filename"]))


def _forwardable_message(message: str) -> str:
    """A top-level message travels only when proven code, like a log template.

    Proven means a source literal of a frame on this stack, or
    :class:`ForwardableText` an allowlist built. Anything else, such as
    ``capture_message(f"now playing {title}")``, is the fixed placeholder.
    """
    if isinstance(message, ForwardableText) or _is_source_literal_on_stack(message):
        return scrub_string(message)
    return LOG_TEXT_WITHHELD


def forwardable_message(message: str) -> str:
    """Public form of the top-level message rule, for identity hashing (sink.py)."""
    return _forwardable_message(message)


def _scrub_message(event: MutableMapping[str, Any]) -> None:
    """A mapping message is rebuilt from ``formatted`` alone; its ``message``
    template and ``params`` carry no provenance here, so they never travel."""
    message = event.get("message")
    if isinstance(message, str):
        event["message"] = _forwardable_message(message)
    elif isinstance(message, dict):
        formatted = message.get("formatted")
        text = _forwardable_message(formatted) if isinstance(formatted, str) else LOG_TEXT_WITHHELD
        event["message"] = {"formatted": text}


def _logentry_template_is_proven(logentry: Mapping[str, Any], hint: Mapping[str, Any]) -> bool:
    """The logging integration hands over its record; nothing else proves a template."""
    record = hint.get("log_record")
    return (
        isinstance(record, logging.LogRecord)
        and _template_is_source_literal(record)
        and logentry.get("message") == record.msg
    )


def _scrub_logentry(event: MutableMapping[str, Any], hint: Mapping[str, Any]) -> None:
    """A logged message travels as template + params + formatted, not `message`.

    Until Mon 14 Sep 2026 this block was never scrubbed, and the top issue in
    the live org carried a raw /Users/... path in its title this way.

    The template (``message``) travels only when the hint's log record proves
    it a source literal (:func:`_template_is_source_literal`); params prove
    nothing, since a runtime-built template takes them as readily. Otherwise
    it is the fixed placeholder. The rendered text never travels, and a
    parameter travels only when it is a plain number.
    """
    logentry = event.get("logentry")
    if not isinstance(logentry, dict):
        return
    if "message" in logentry or "formatted" in logentry:
        logentry["message"] = (
            scrub_string(str(logentry["message"]))
            if _logentry_template_is_proven(logentry, hint)
            else LOG_TEXT_WITHHELD
        )
    logentry.pop("formatted", None)
    params = logentry.get("params")
    if isinstance(params, (list, tuple)):
        logentry["params"] = [value if type(value) in (int, float, bool) else LOG_ARG_WITHHELD for value in params]
    elif params is not None:
        logentry["params"] = LOG_ARG_WITHHELD


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
    """Only a message ``before_breadcrumb`` built travels; an event can carry
    breadcrumbs that never passed through it."""
    for crumb in _values(event.get("breadcrumbs")):
        if not isinstance(crumb, dict):
            continue
        message = crumb.get("message")
        if isinstance(message, str):
            crumb["message"] = (
                scrub_string(message) if isinstance(message, ForwardableText) else BREADCRUMB_TEXT_WITHHELD
            )
        if crumb.get("data") is not None:
            crumb["data"] = _allowlist(crumb["data"])


def scrub_event(
    event: MutableMapping[str, Any],
    hint: Mapping[str, Any] | None = None,
) -> MutableMapping[str, Any] | None:
    """Strip library content and secrets. Drops the event if scrubbing fails.

    Fail-closed on purpose: an event that could not be scrubbed is an event
    whose contents are unknown, and the safe thing to do with unknown
    contents is not send them.
    """
    try:
        _scrub_sections(event)
        _scrub_exception(event)
        _scrub_stacktraces(event)
        _scrub_message(event)
        _scrub_logentry(event, hint or {})
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


# ----- privacy-safe bug-report bundle (issue #11173) ----------------------
BUG_REPORT_TEXT_MAX_BYTES: int = 256 * 1024

#: Keys a CLI or UI diagnostics bundle may carry verbatim (booleans and
#: machine facts only). Everything else becomes :data:`REDACTED`, matching the
#: telemetry allowlist style so a new field is invisible until somebody adds it
#: on purpose.
BUG_REPORT_ALLOWED_KEYS: frozenset[str] = frozenset(
    ALLOWED_CONTEXT_KEYS
    | {
        # conn-dot / connectivity (states only, never library paths)
        "conn_dot_fe",
        "conn_dot_be",
        "conn_dot_lib",
        "conn_dot_spotify",
        "conn_dot_rekordbox",
        "conn_dot_djay",
        "conn_dots",
        "client_errors",
        "health",
        "engine_reachable",
        "client_open",
        "mirror_stale",
        "decks_busy",
        # ports and identity
        "backend_port",
        "frontend_port",
        "git_sha",
        # health summaries (counts, never paths)
        "tracks",
        "tracks_playable",
        "playlists",
        "pairings",
        "bind_host",
        "google_oauth_configured",
        "process_env_home_present",
        "peers_connected",
        "folder_state",
        "native_available",
        # prefs: only boolean leaves are harvested; keys still pass the allowlist
        "theme_dark",
        "midi_enabled",
        "telemetry_consent_accepted",
        "midi_enabled",
        "theme_dark",
        "google_oauth_enabled",
        "deck_autocue",
        "prefs_booleans",
        "schema",
        "generated_at",
        "refusal",
        "reason",
    }
)

#: Substrings that must never appear in shareable bundle text even when a path
#: scrubber might miss an edge case (ANLZ payloads, USB inventories, DB dumps).
_BUG_REPORT_FORBIDDEN_RE = re.compile(
    r"(?:\bANLZ\b|"
    r"\bUSB[_-]?EXPORT\b|"
    r"\bdjmd(?:Content|Playlist|Song)\b|"
    r"\bPCOB\b|"
    r"\bBEGIN\s+TRANSACTION\b.*\bCREATE\s+TABLE\b|"
    r"\baudio[_-]?content[_-]?hash\b|"
    r"\bsha256:[0-9a-f]{64}\b)",
    re.IGNORECASE | re.DOTALL,
)


def _bug_report_allowlist(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name.lower() not in BUG_REPORT_ALLOWED_KEYS:
                out[name] = REDACTED
            elif isinstance(item, bool) or item is None or isinstance(item, int | float):
                out[name] = item
            elif isinstance(item, str):
                out[name] = scrub_string(item)
            else:
                out[name] = _bug_report_allowlist(item)
        return out
    if isinstance(value, list):
        return [_bug_report_allowlist(item) for item in value]
    if isinstance(value, str):
        return scrub_string(value)
    return value


def scrub_bug_report_mapping(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Allowlisted mapping for ``scripts/bug_report_bundle`` and tests."""
    if not isinstance(value, Mapping):
        raise TypeError("bug report payload must be a mapping")
    scrubbed = _bug_report_allowlist(dict(value))
    assert isinstance(scrubbed, dict)
    return scrubbed


def truncate_bug_report_text(text: str) -> str:
    """Hard cap at :data:`BUG_REPORT_TEXT_MAX_BYTES` UTF-8 bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) <= BUG_REPORT_TEXT_MAX_BYTES:
        return text
    clipped = encoded[:BUG_REPORT_TEXT_MAX_BYTES]
    while clipped and (clipped[-1] & 0xC0) == 0x80:
        clipped = clipped[:-1]
    return clipped.decode("utf-8", errors="strict")


def assert_bug_report_text_safe(text: str) -> None:
    """Fail closed when forbidden classes or un-allowlisted secret shapes remain."""
    if _BUG_REPORT_FORBIDDEN_RE.search(text):
        raise ValueError("bug report text matched a forbidden diagnostics class")
    if _TOKENISH_RE.search(text):
        raise ValueError("bug report text matched a secret-shaped substring")
