"""OBS-6393: library content never rides free text off the machine.

Three channels carry free text to Sentry: an ERROR log forwarded by the
engine warning log, the log breadcrumbs attached to every event, and the
message of a captured exception. Each test below sends an artist, a title
with spaces and a track filename down one channel through the REAL SDK
pipeline (``init_telemetry``, its ``before_send`` and ``before_breadcrumb``,
an in-memory transport) and then searches the WHOLE serialized envelope,
not named fields, because a leak in a field nobody thought to check is the
failure this guards against.

Every channel runs through the production scrubber only; nothing monkeypatches
``scrub_string`` away. Extensionless titles such as ``TITLE`` below are the
cases shape-based filtering alone cannot recognize.

A template travels only when it is PROVEN a source literal of the calling
code; a template built at run time (an f-string that also takes arguments)
is withheld like a pre-rendered line. Every stack in an event drops the
home directory, whether or not an exception value sits beside it.

[if] a log line, a breadcrumb or an exception message names library content [then] fail if any of it reaches the serialized Sentry event, [else stop].
"""

from __future__ import annotations

import enum
import json
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from apps.engine_core.warning_log import _WarningJsonHandler
from apps.shared.telemetry import (
    BREADCRUMB_TEXT_WITHHELD,
    BROWSER_ERROR_NAME_WITHHELD,
    BROWSER_FRAME_WITHHELD,
    DSN_ENV,
    EXCEPTION_VALUE_WITHHELD,
    LOG_ARG_WITHHELD,
    LOG_TEXT_WITHHELD,
    TELEMETRY_ENV,
    capture_browser_error,
    decide_telemetry,
    forwardable_log_message,
    init_telemetry,
    reset_live_gate_for_tests,
    scrub,
    scrub_event,
)
from apps.shared.telemetry.budget import SENTRY_BUDGET
from apps.shared.telemetry.scrub import EXCEPTION_TYPE_WITHHELD
from tests.support.sentry_client import close_sentry_client

pytestmark = [pytest.mark.no_leaked_threads, pytest.mark.requirement("OBS-6393")]

DSN = "https://public@o0.ingest.de.sentry.io/1"
SHA = "0123456789abcdef0123456789abcdef01234567"
LOGGER_NAME = "tests.telemetry.content_allowlist"

#: Invented library content. The filename has spaces and no directory, which
#: is the shape the path filter alone lets through.
ARTIST = "Zelda Quillfeather"
TITLE = "Umbral Kite Parade"
FILE_NAME = f"{ARTIST} - {TITLE}.mp3"
#: Whole strings AND their distinctive words: a partial leak is still a leak.
LIBRARY_STRINGS: tuple[str, ...] = (
    ARTIST,
    TITLE,
    FILE_NAME,
    "Zelda",
    "Quillfeather",
    "Umbral",
    "Kite",
)
#: Library content spelled INTO a template at run time.
RUNTIME_TITLE = "Fixture Artist - Fixture Title"
RUNTIME_TITLE_STRINGS: tuple[str, ...] = (RUNTIME_TITLE, "Fixture Artist", "Fixture Title")
#: A home directory, and with it an account name, that must never travel.
FAKE_HOME = "/Users/someone-else"
PROBE_LOGGER_NAME = f"{LOGGER_NAME}.probe"


class FixtureTrackError(RuntimeError):
    """Stands in for the analysis errors that name the file they failed on."""


class _Deck(enum.IntEnum):
    """An argument ``%d`` accepts that is still not a plain ``int``."""

    LEFT = 2


def _assert_no_library_content(serialized: str, where: str, strings: tuple[str, ...] = LIBRARY_STRINGS) -> None:
    leaked = [text for text in strings if text in serialized]
    assert leaked == [], f"if {where} carries {leaked} off the machine then broken"


class _ForwardedText(logging.Handler):
    """Collects ``forwardable_log_message`` from inside the real logging call."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.texts: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.texts.append(forwardable_log_message(record))


@contextmanager
def _logger_with(name: str, handler: logging.Handler) -> Iterator[logging.Logger]:
    """A non-propagating INFO logger with exactly ``handler`` added."""
    logger = logging.getLogger(name)
    saved_level, saved_propagate = logger.level, logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield logger
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


# The functions that raise sit far below the constants above on purpose: the
# SDK ships a few source lines around every stack frame, and those lines must
# not be the ones that spell the fixture strings out.


def _raise_with_library_locals() -> None:
    artist_local = ARTIST
    title_local = TITLE
    raise FixtureTrackError(f"{FILE_NAME}: unreadable ({artist_local} / {title_local})")


@pytest.fixture
def sentry_events() -> Iterator[Callable[[], list[dict[str, Any]]]]:
    """Production init against the SDK's in-memory transport."""
    try:
        import sentry_sdk
    except ImportError:
        # CI installs the observability extra (requirements.txt via scripts/ci_lock.py; airgap-mac
        # uses --all-extras), so there a missing SDK is a hard failure, never a silent skip.
        if os.environ.get("CI"):
            pytest.fail("UNAVAILABLE: sentry_sdk missing in CI; OBS-6393 SDK acceptance cannot run")
        pytest.skip("needs the optional observability extra (fails instead of skipping when CI is set)")
    from sentry_sdk.transport import Transport

    captured: list[dict[str, Any]] = []

    class CapturingTransport(Transport):
        def capture_envelope(self, envelope) -> None:  # type: ignore[no-untyped-def]
            for item in envelope.items:
                payload = item.payload.json
                if payload is not None:
                    captured.append(payload)

    def flushed() -> list[dict[str, Any]]:
        sentry_sdk.flush()
        return captured

    SENTRY_BUDGET.reset()
    reset_live_gate_for_tests()
    decision = decide_telemetry({TELEMETRY_ENV: "1", DSN_ENV: DSN}, build_source="payload", release=SHA)
    try:
        assert init_telemetry(decision, transport=CapturingTransport()) is True
        yield flushed
    finally:
        close_sentry_client()
        SENTRY_BUDGET.reset()
        reset_live_gate_for_tests()


@pytest.fixture
def engine_logger(tmp_path: Path) -> Iterator[tuple[logging.Logger, Path]]:
    """A logger wired to the engine's own warning-log handler."""
    warn_log = tmp_path / "engine-warn.log"
    with _logger_with(LOGGER_NAME, _WarningJsonHandler(warn_log, "boot-allowlist")) as logger:
        yield logger, warn_log


# ----- channel 1: ERROR logs forwarded by the warning log -------------------
def test_forwarded_error_logs_carry_the_template_and_never_the_rendered_text(
    sentry_events: Callable[[], list[dict[str, Any]]],
    engine_logger: tuple[logging.Logger, Path],
    tmp_path: Path,
) -> None:
    logger, warn_log = engine_logger

    logger.error("decode failed for %s after %d attempts", FILE_NAME, 3)
    logger.error(f"could not load {TITLE} by {ARTIST}")  # noqa: G004 - deliberate bad-call fixture
    try:
        _raise_with_library_locals()
    except FixtureTrackError:
        logger.exception(f"analysis of {FILE_NAME} failed")  # noqa: G004 - deliberate bad-call fixture

    events = sentry_events()
    serialized = json.dumps(events)
    assert len(events) == 3, "if an ERROR log stops reaching the transport then broken"
    _assert_no_library_content(serialized, "a forwarded ERROR log")

    messages = [event["message"] for event in events]
    assert f"decode failed for {LOG_ARG_WITHHELD} after 3 attempts" in messages, (
        "if a templated record loses its template or its numeric argument then broken"
    )
    withheld = [message for message in messages if LOG_TEXT_WITHHELD in message]
    assert len(withheld) == 2, "if a template-less record sends anything but the placeholder then broken"
    assert all(message.startswith(f"ERROR {LOGGER_NAME}: ") for message in withheld), (
        "if a template-less record drops its level or logger name then broken"
    )
    assert any(message.endswith("[FixtureTrackError]") for message in withheld), (
        "if a logged exception drops its type name then broken"
    )
    assert all(event["tags"]["source_site"] == f"engine:{LOGGER_NAME}" for event in events)

    # The other direction: nothing is taken away from the LOCAL record.
    local_lines = warn_log.read_text(encoding="utf-8")
    assert FILE_NAME in local_lines and TITLE in local_lines, (
        "if the local warning log loses the rendered text then broken"
    )
    sink_rows = (tmp_path / "opendj-error-sink.jsonl").read_text(encoding="utf-8")
    assert FILE_NAME in sink_rows, "if the local error sink loses the rendered text then broken"


# ----- channel 2: breadcrumbs ----------------------------------------------
def test_breadcrumbs_carry_the_template_and_never_the_rendered_text(
    sentry_events: Callable[[], list[dict[str, Any]]],
    engine_logger: tuple[logging.Logger, Path],
) -> None:
    import sentry_sdk

    logger, _ = engine_logger
    logger.info("loaded %s at %d Hz", FILE_NAME, 44100)
    logger.info(f"now playing {TITLE} by {ARTIST}")  # noqa: G004 - deliberate bad-call fixture
    logger.warning("retrying %(what)s, attempt %(attempt)d", {"what": TITLE, "attempt": 2})
    sentry_sdk.add_breadcrumb(category="manual", message=f"queued {FILE_NAME}")
    sentry_sdk.capture_message("allowlist breadcrumb probe", level="error")

    events = sentry_events()
    serialized = json.dumps(events)
    assert len(events) == 1
    _assert_no_library_content(serialized, "a breadcrumb")

    crumbs = events[0]["breadcrumbs"]["values"]
    by_message = {crumb["message"]: crumb for crumb in crumbs}
    templated = by_message.get(f"loaded {LOG_ARG_WITHHELD} at 44100 Hz")
    assert templated is not None, (
        f"if a log breadcrumb loses its template or numeric argument then broken: {list(by_message)}"
    )
    assert templated["category"] == LOGGER_NAME and templated["level"] == "info"
    assert f"retrying {LOG_ARG_WITHHELD}, attempt 2" in by_message
    assert any(message.startswith(f"INFO {LOGGER_NAME}: {LOG_TEXT_WITHHELD}") for message in by_message), (
        "if a template-less breadcrumb drops its level or logger name then broken"
    )
    assert by_message[BREADCRUMB_TEXT_WITHHELD]["category"] == "manual"


def test_a_breadcrumb_sent_inside_the_event_is_withheld_too(
    sentry_events: Callable[[], list[dict[str, Any]]],
) -> None:
    import sentry_sdk

    sentry_sdk.capture_event(
        {
            "level": "error",
            "message": "allowlist inline breadcrumb probe",
            "breadcrumbs": {"values": [{"category": "manual", "message": f"now playing {TITLE}"}]},
        }
    )

    events = sentry_events()
    assert len(events) == 1
    _assert_no_library_content(json.dumps(events), "a breadcrumb that skipped before_breadcrumb")
    messages = [crumb["message"] for crumb in events[0]["breadcrumbs"]["values"]]
    assert BREADCRUMB_TEXT_WITHHELD in messages, f"if it stops traveling as the placeholder then broken: {messages}"


# ----- channel 3: exception messages (and frame locals) --------------------
def test_exceptions_carry_type_and_stack_and_never_the_message_or_locals(
    sentry_events: Callable[[], list[dict[str, Any]]],
) -> None:
    import sentry_sdk

    try:
        _raise_with_library_locals()
    except FixtureTrackError as exc:
        sentry_sdk.capture_exception(exc)

    events = sentry_events()
    serialized = json.dumps(events)
    assert len(events) == 1
    _assert_no_library_content(serialized, "a captured exception")

    entry = events[0]["exception"]["values"][0]
    assert entry["type"] == "FixtureTrackError"
    assert entry["module"] == __name__
    assert entry["value"] == EXCEPTION_VALUE_WITHHELD
    frames = entry["stacktrace"]["frames"]
    assert frames[-1]["function"] == "_raise_with_library_locals", (
        "if the stack stops naming the raising function then broken"
    )
    assert all("vars" not in frame for frame in frames), "if frame-local variables travel then broken"
    assert frames[-1]["filename"].endswith("test_telemetry_content_allowlist.py"), (
        "if a frame stops naming its source file then broken"
    )
    assert str(Path.home()) not in serialized, (
        "if a stack frame carries the home directory, and with it the account name, then broken"
    )
    assert sentry_sdk.get_client().options["include_local_variables"] is False, (
        "if the SDK is set to capture frame locals then broken"
    )


def test_an_exception_entry_sends_only_its_allowlisted_fields(
    sentry_events: Callable[[], list[dict[str, Any]]],
) -> None:
    import sentry_sdk

    sentry_sdk.capture_event(
        {
            "level": "error",
            "exception": {
                "values": [
                    {
                        "type": "FixtureTrackError",
                        "module": __name__,
                        "value": TITLE,
                        "note": f"now playing {ARTIST}",
                        "mechanism": {
                            "type": "generic",
                            "handled": False,
                            "description": FILE_NAME,
                            "data": {"t": TITLE},
                        },
                    }
                ]
            },
        }
    )

    events = sentry_events()
    assert len(events) == 1
    _assert_no_library_content(json.dumps(events), "an exception entry's free-text fields")
    entry = events[0]["exception"]["values"][0]
    assert entry["type"] == "FixtureTrackError" and entry["mechanism"] == {"type": "generic", "handled": False}, (
        f"if the type or the mechanism's structural fields stop traveling then broken: {entry}"
    )


def _raise(exc: BaseException) -> None:
    raise exc


def test_a_runtime_built_exception_class_name_never_travels(
    sentry_events: Callable[[], list[dict[str, Any]]],
) -> None:
    """[if] an exception class is built at run time from library text [then] its type, module and error id carry none of it, [else stop]."""
    import sentry_sdk

    dynamic = type(TITLE, (Exception,), {"__module__": f"{__name__}.{ARTIST}"})
    try:
        _raise(dynamic("decode failed"))
    except dynamic as exc:
        sentry_sdk.capture_exception(exc)
    sentry_sdk.capture_event({"level": "error", "exception": {"values": [{"type": ARTIST, "value": "x"}]}})

    events = sentry_events()
    assert len(events) == 2
    _assert_no_library_content(json.dumps(events), "a runtime-built exception class")
    for event in events:
        entry = event["exception"]["values"][0]
        assert entry["type"] == EXCEPTION_TYPE_WITHHELD and "module" not in entry, (
            f"if a dynamic class name or module travels then broken: {entry}"
        )
    assert all(event["tags"].get("error_id") for event in events), (
        "if the withheld type stops yielding an error id then broken"
    )


def test_a_builtin_exception_type_still_travels(
    sentry_events: Callable[[], list[dict[str, Any]]],
) -> None:
    """[if] a builtin exception is captured [then] its type still travels (control), [else stop]."""
    import sentry_sdk

    try:
        _raise(ValueError(TITLE))
    except ValueError as exc:
        sentry_sdk.capture_exception(exc)

    events = sentry_events()
    assert len(events) == 1
    assert events[0]["exception"]["values"][0]["type"] == "ValueError", (
        "if a builtin exception type stops traveling then broken"
    )
    _assert_no_library_content(json.dumps(events), "a builtin exception")


def test_exception_error_id_does_not_hash_the_rendered_exception_value(
    sentry_events: Callable[[], list[dict[str, Any]]],
) -> None:
    """if exception text changes then its Sentry identity stays type/site based."""
    import sentry_sdk

    sentry_sdk.capture_event(
        {
            "level": "error",
            "exception": {"values": [{"type": "FixtureTrackError", "value": FILE_NAME}]},
        }
    )
    sentry_sdk.capture_event(
        {
            "level": "error",
            "exception": {"values": [{"type": "FixtureTrackError", "value": TITLE}]},
        }
    )

    events = sentry_events()
    assert len(events) == 2
    first, second = events
    assert first["tags"]["error_id"] == second["tags"]["error_id"]
    assert first["fingerprint"] == second["fingerprint"]
    _assert_no_library_content(json.dumps(events), "an exception error id")


# ----- template provenance: proven literals only ----------------------------
def test_a_template_built_at_run_time_is_withheld_and_a_literal_one_still_travels(
    sentry_events: Callable[[], list[dict[str, Any]]],
    engine_logger: tuple[logging.Logger, Path],
) -> None:
    logger, _ = engine_logger
    title = RUNTIME_TITLE
    exc = FixtureTrackError(FILE_NAME)

    logger.error(f"{title}: %s", exc)  # noqa: G004 - deliberate bad-call fixture
    logger.error("decode failed for track %s", title)

    events = sentry_events()
    serialized = json.dumps(events)
    assert len(events) == 2, "if an ERROR log stops reaching the transport then broken"
    _assert_no_library_content(serialized, "a runtime-built template", RUNTIME_TITLE_STRINGS)
    _assert_no_library_content(serialized, "a runtime-built template's argument")

    messages = [event["message"] for event in events]
    assert messages[1] == f"decode failed for track {LOG_ARG_WITHHELD}", (
        "if a literal template stops traveling, or its text argument does, then broken"
    )
    withheld = messages[0]
    location = f"({Path(__file__).name}:"
    assert withheld.startswith(f"ERROR {LOGGER_NAME}: {LOG_TEXT_WITHHELD} {location}"), (
        f"if a runtime-built template sends anything but the placeholder then broken: {withheld}"
    )
    assert withheld.endswith(" in test_a_template_built_at_run_time_is_withheld_and_a_literal_one_still_travels)")
    assert "/" not in withheld and "\\" not in withheld, (
        "if the placeholder names a directory, not the file's basename, then broken"
    )


def test_a_top_level_message_built_at_run_time_is_withheld_and_a_literal_one_still_travels(
    sentry_events: Callable[[], list[dict[str, Any]]],
) -> None:
    import sentry_sdk

    sentry_sdk.capture_message(f"now playing {TITLE} by {ARTIST}", level="error")
    sentry_sdk.capture_message("deck A decode stalled", level="error")

    events = sentry_events()
    _assert_no_library_content(json.dumps(events), "a top-level message")
    assert [event["message"] for event in events] == [
        LOG_TEXT_WITHHELD,
        "deck A decode stalled",
    ], "if a runtime-built message travels, or a literal one stops traveling, then broken"


def test_a_runtime_built_top_level_message_never_feeds_the_error_id(
    sentry_events: Callable[[], list[dict[str, Any]]],
) -> None:
    """The id hashes the forwarded placeholder, so two titles cannot be told apart by id."""
    import sentry_sdk

    from apps.shared.telemetry.error_id import stable_error_id
    from apps.shared.telemetry.sink import event_error_id

    for title in (TITLE, "Another Fixture Title"):
        sentry_sdk.capture_message(f"now playing {title}", level="error")

    events = sentry_events()
    ids = [event_error_id(event) for event in events]
    fingerprints = [event.get("fingerprint") for event in events]
    assert len(ids) == 2 and ids[0] and ids[0] == ids[1], (
        "if the error id varies with the runtime title then the title leaks through the id"
    )
    assert fingerprints[0] == fingerprints[1]
    raw = stable_error_id(source_site="engine:uncaught", message=f"now playing {TITLE}")
    assert ids[0] != raw, "if the id is the hash of the raw message then broken"


def _real_local_sink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the production JSONL sink at ``tmp_path``; nothing is monkeypatched away."""
    from apps.shared.telemetry.sink import ERROR_SINK_ENV, reset_rate_state_for_tests

    sink_file = tmp_path / "error-sink.jsonl"
    monkeypatch.setenv(ERROR_SINK_ENV, str(sink_file))
    reset_rate_state_for_tests()
    return sink_file


def _sink_rows(sink_file: Path) -> list[dict[str, Any]]:
    """The JSONL rows the real sink wrote."""
    if not sink_file.exists():
        return []
    return [json.loads(line) for line in sink_file.read_text(encoding="utf-8").splitlines() if line]


def test_the_local_sink_keeps_raw_text_the_forwarded_id_never_sees(
    sentry_events: Callable[[], list[dict[str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] a runtime message is captured [then] the local JSONL has it and the event does not."""
    import sentry_sdk

    sink_file = _real_local_sink(tmp_path, monkeypatch)
    sentry_sdk.capture_message(f"now playing {TITLE}", level="error")
    events = sentry_events()
    rows = _sink_rows(sink_file)
    assert [row["message"] for row in rows] == [f"now playing {TITLE}"], "if the local sink loses raw text then broken"
    _assert_no_library_content(json.dumps(events), "the forwarded event")


def test_the_local_sink_keeps_raw_browser_text_even_when_nothing_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a browser error is reported with telemetry off [then] the local row keeps its text."""
    sink_file = _real_local_sink(tmp_path, monkeypatch)
    capture_browser_error(
        message=f"could not load {FILE_NAME}",
        name="TypeError",
        stack="",
        url="http://127.0.0.1:5173/performance",
        user_agent="Mozilla/5.0",
        context={"kind": "window-error"},
    )
    rows = _sink_rows(sink_file)
    assert len(rows) == 1 and FILE_NAME in rows[0]["message"], "if the local sink loses browser text then broken"
    assert FILE_NAME not in rows[0]["error_id"]


def test_a_build_failure_id_never_derives_from_its_dynamic_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] build output names library content [then] its id and event do not, and the local row keeps it."""
    from apps.shared.telemetry.sink import post_build_failure

    sink_file = _real_local_sink(tmp_path, monkeypatch)
    first = post_build_failure(message=f"dmg failed on {TITLE}")
    second = post_build_failure(message=f"dmg failed on {FILE_NAME}")
    assert first.error_id == second.error_id, "if the build id is derived from dynamic text then broken"
    _assert_no_library_content(json.dumps([first.as_dict(), second.as_dict()]), "the build failure event")
    rows = _sink_rows(sink_file)
    assert [TITLE in rows[0]["message"], FILE_NAME in rows[1]["message"]] == [True, True], (
        "if the local sink loses raw build text then broken"
    )


def test_a_logentry_without_its_log_record_sends_the_placeholder(
    sentry_events: Callable[[], list[dict[str, Any]]],
) -> None:
    import sentry_sdk

    sentry_sdk.capture_event(
        {
            "level": "error",
            "logentry": {
                "message": f"could not load {TITLE} by {ARTIST}, attempt %d",
                "params": [2],
                "formatted": f"could not load {TITLE} by {ARTIST}, attempt 2",
            },
        }
    )

    events = sentry_events()
    serialized = json.dumps(events)
    assert len(events) == 1
    _assert_no_library_content(serialized, "a logentry with no record behind it")
    assert events[0]["logentry"] == {"message": LOG_TEXT_WITHHELD, "params": [2]}, (
        "if params alone are taken as proof that a logentry message is a template then broken"
    )


def test_logentry_keeps_a_proven_template_and_numbers_only(
    sentry_events: Callable[[], list[dict[str, Any]]],
) -> None:
    from sentry_sdk.integrations.logging import EventHandler

    with _logger_with(PROBE_LOGGER_NAME, EventHandler(level=logging.ERROR)) as logger:
        logger.error("decode failed %s after %d tries", FILE_NAME, 3)
        logger.error(f"decode failed {FILE_NAME} after %d tries", 3)  # noqa: G004 - deliberate bad-call fixture

    events = sentry_events()
    _assert_no_library_content(json.dumps(events), "a logentry")
    assert [event["logentry"] for event in events] == [
        {"message": "decode failed %s after %d tries", "params": [LOG_ARG_WITHHELD, 3]},
        {"message": LOG_TEXT_WITHHELD, "params": [3]},
    ], "if the SDK's own log event keeps a runtime-built template, or loses a literal one, then broken"


# ----- every stack, value or not ---------------------------------------------
def _stack_under_fake_home(function: str) -> dict[str, Any]:
    root = f"{FAKE_HOME}/code/opendj"
    return {
        "frames": [
            {
                "filename": f"{root}/run.py",
                "abs_path": f"{root}/run.py",
                "function": "main",
                "lineno": 3,
            },
            {
                "filename": "apps/analysis/decode.py",
                "abs_path": f"{root}/apps/analysis/decode.py",
                "module": "apps.analysis.decode",
                "function": function,
                "lineno": 12,
            },
        ]
    }


def test_every_stack_drops_the_home_directory_with_or_without_an_exception_value(
    sentry_events: Callable[[], list[dict[str, Any]]],
) -> None:
    import sentry_sdk

    sentry_sdk.capture_event(
        {
            "level": "error",
            "exception": {
                "values": [
                    {
                        "type": "FixtureTrackError",
                        "module": __name__,
                        "stacktrace": _stack_under_fake_home("raise_site"),
                    }
                ]
            },
            "stacktrace": _stack_under_fake_home("event_site"),
            "threads": {"values": [{"id": 1, "current": True, "stacktrace": _stack_under_fake_home("thread_site")}]},
        }
    )

    events = sentry_events()
    serialized = json.dumps(events)
    assert len(events) == 1
    assert FAKE_HOME not in serialized and "someone-else" not in serialized, (
        "if any stack in the event carries the home directory, and with it the account name, then broken"
    )
    event = events[0]
    stacks = [
        event["exception"]["values"][0]["stacktrace"],
        event["stacktrace"],
        event["threads"]["values"][0]["stacktrace"],
    ]
    assert [stack["frames"][-1]["function"] for stack in stacks] == [
        "raise_site",
        "event_site",
        "thread_site",
    ], "if a scrubbed stack stops naming its functions then broken"
    assert all(
        [frame["filename"] for frame in stack["frames"]] == ["run.py", "apps/analysis/decode.py"] for stack in stacks
    ), "if a frame stops naming its file, or keeps an absolute one, then broken"
    assert "value" not in event["exception"]["values"][0]


# ----- a malformed call is the caller's bug, not ours to hide ---------------
def test_a_malformed_logging_call_fails_exactly_as_it_does_without_telemetry(
    sentry_events: Callable[[], list[dict[str, Any]]],
    engine_logger: tuple[logging.Logger, Path],
) -> None:
    malformed = "deck %d lost %s"
    with pytest.raises(TypeError) as without_telemetry:
        logging.makeLogRecord({"msg": malformed, "args": (2,)}).getMessage()

    logger, _ = engine_logger
    with pytest.raises(TypeError) as engine:
        logger.error(malformed, 2)
    probe = _ForwardedText()
    with _logger_with(PROBE_LOGGER_NAME, probe) as probe_logger, pytest.raises(TypeError) as forwarded:
        probe_logger.error(malformed, 2)

    assert str(forwarded.value) == str(engine.value) == str(without_telemetry.value), (
        "if the telemetry rendition turns a malformed logging call into anything else then broken"
    )
    assert probe.texts == []
    assert sentry_events() == []


# ----- the pieces, as policy ------------------------------------------------
def _record(msg: object, args: object, *, exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    return logging.LogRecord(
        name=LOGGER_NAME,
        level=logging.ERROR,
        pathname=__file__,
        lineno=7,
        msg=msg,
        args=args,  # type: ignore[arg-type]
        exc_info=exc_info,
        func="some_function",
    )


def test_a_template_keeps_plain_numbers_and_withholds_every_other_argument() -> None:
    probe = _ForwardedText()
    with _logger_with(PROBE_LOGGER_NAME, probe) as logger:
        logger.error("%s took %.1f ms over %d tries (ok=%s)", TITLE, 12.5, 4, True)
        logger.error("tags %s", {"title": TITLE})
        logger.error("ready")
    assert probe.texts == [
        f"{LOG_ARG_WITHHELD} took 12.5 ms over 4 tries (ok=True)",
        f"tags {LOG_ARG_WITHHELD}",
        "ready",
    ]


def test_a_numeric_directive_given_a_withheld_argument_sends_the_bare_template() -> None:
    probe = _ForwardedText()
    with _logger_with(PROBE_LOGGER_NAME, probe) as logger:
        logger.error("deck %d failed", _Deck.LEFT)
        with pytest.raises(TypeError):
            logger.error("deck %d failed", TITLE)
    assert probe.texts == ["deck %d failed"]


def test_a_literal_template_handled_away_from_its_call_is_withheld() -> None:
    message = forwardable_log_message(_record("decode failed for track %s", (TITLE,)))
    assert message == (f"ERROR {LOGGER_NAME}: {LOG_TEXT_WITHHELD} ({Path(__file__).name}:7 in some_function)")


def test_a_record_without_arguments_never_sends_its_text() -> None:
    message = forwardable_log_message(_record(f"could not load {TITLE}", ()))
    assert TITLE not in message
    assert message == (f"ERROR {LOGGER_NAME}: {LOG_TEXT_WITHHELD} ({Path(__file__).name}:7 in some_function)")


def test_a_non_string_message_is_treated_as_having_no_template() -> None:
    message = forwardable_log_message(_record(FixtureTrackError(FILE_NAME), ()))
    _assert_no_library_content(message, "a record whose msg is an object")
    assert LOG_TEXT_WITHHELD in message


def test_exception_messages_are_withheld_while_the_allowlist_is_empty() -> None:
    """Production policy only: no type is allowlisted, so every value is withheld.

    The allowlisted branch gets its test when an audited type genuinely joins
    SAFE_EXCEPTION_TYPES; mutating module state here would not exercise production."""
    event: dict[str, Any] = {"exception": {"values": [{"type": "FixtureTrackError", "value": "took 12 ms"}]}}
    result = scrub_event(event)
    assert result is not None
    assert frozenset() == scrub.SAFE_EXCEPTION_TYPES, (
        "if a type joins the allowlist without its raise sites being audited then broken"
    )
    assert result["exception"]["values"][0]["value"] == EXCEPTION_VALUE_WITHHELD


# ----- browser stacks, structured messages, frame source context ------------
BROWSER_FRAMES = (
    "    at decodeTrack (http://127.0.0.1:5173/src/lib/audio.ts:41:9)",
    "    at http://127.0.0.1:5173/src/app.ts:7:3",
    "decodeTrack@http://127.0.0.1:5173/src/lib/audio.ts:41:9",
)


def test_a_browser_stack_sends_its_frame_lines_and_never_its_message_header(
    sentry_events: Callable[[], list[dict[str, Any]]],
) -> None:
    header = (f"TypeError: could not load {FILE_NAME}", f"    {TITLE} by {ARTIST}")
    stack = "\n".join((*header, *BROWSER_FRAMES))
    event_id = capture_browser_error(
        message=f"could not load {FILE_NAME}",
        name="TypeError",
        stack=stack,
        url="http://127.0.0.1:5173/performance",
        user_agent="Mozilla/5.0",
        context={
            "kind": "window-error",
            "any_deck_live": False,
            "fallback_message": f"now playing {TITLE} by {ARTIST}",
        },
    )
    assert event_id is not None, "if the browser error stops reaching Sentry then broken"

    events = sentry_events()
    assert len(events) == 1
    _assert_no_library_content(json.dumps(events), "a browser stack header or fallback_message")
    body = str(events[0]["message"])
    rebuilt = ("    at /src/lib/audio.ts:41:9", "    at /src/app.ts:7:3")
    assert body.splitlines()[1:] == [rebuilt[0], rebuilt[1], rebuilt[0]], (
        f"if a V8 or Firefox/Safari frame stops traveling as its code path and position then broken: {body}"
    )
    assert "decodeTrack" not in body and "127.0.0.1" not in body, (
        f"if a frame's function name or origin travels then broken: {body}"
    )


def test_a_page_chosen_error_name_and_frame_url_are_withheld(
    sentry_events: Callable[[], list[dict[str, Any]]],
) -> None:
    secrets = ("Quillfeather", "UmbralKite")
    stack = "\n".join(
        (
            "Quillfeather: boom",
            "Quillfeather@https://app.invalid/now-playing/UmbralKite:1:2",
            "    at UmbralKite (blob:https://app.invalid/UmbralKite:3:4)",
        )
    )
    capture_browser_error(
        message="boom",
        name="Quillfeather",
        stack=stack,
        url="http://127.0.0.1:5173/performance",
        user_agent="Mozilla/5.0",
        context={"kind": "window-error", "any_deck_live": False},
    )

    events = sentry_events()
    assert len(events) == 1
    _assert_no_library_content(json.dumps(events), "a custom error name or route frame", secrets)
    body = str(events[0]["message"])
    assert BROWSER_ERROR_NAME_WITHHELD in body, f"if the name stops traveling as the placeholder then broken: {body}"
    assert body.count(BROWSER_FRAME_WITHHELD) == 2, (
        f"if a non-code frame stops traveling as the placeholder then broken: {body}"
    )


def test_a_structured_message_travels_as_its_proven_formatted_text_only(
    sentry_events: Callable[[], list[dict[str, Any]]],
) -> None:
    import sentry_sdk

    sentry_sdk.capture_event(
        {
            "level": "error",
            "message": {
                "formatted": "deck A decode stalled",
                "message": f"now playing {TITLE} by {ARTIST}",
                "params": [FILE_NAME],
            },
        }
    )

    events = sentry_events()
    assert len(events) == 1
    _assert_no_library_content(json.dumps(events), "a structured message's template or params")
    assert events[0]["message"] == {"formatted": "deck A decode stalled"}, (
        "if a structured message keeps a field beside formatted, or loses a literal one, then broken"
    )


def _raise_beside_library_source() -> None:
    note = "Umbral Kite Parade by Zelda Quillfeather"
    raise FixtureTrackError(note)


def test_a_captured_frame_sends_no_source_context(
    sentry_events: Callable[[], list[dict[str, Any]]],
) -> None:
    import sentry_sdk

    try:
        _raise_beside_library_source()
    except FixtureTrackError as exc:
        sentry_sdk.capture_exception(exc)

    events = sentry_events()
    assert len(events) == 1
    _assert_no_library_content(json.dumps(events), "a frame's source context")
    frames = events[0]["exception"]["values"][0]["stacktrace"]["frames"]
    assert frames[-1]["function"] == "_raise_beside_library_source"
    assert isinstance(frames[-1]["lineno"], int), "if a frame loses its line number then broken"
    assert all(
        not {"pre_context", "context_line", "post_context", "abs_path", "vars"} & frame.keys() for frame in frames
    ), "if any source-context, path or locals field travels on a frame then broken"
