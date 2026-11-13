"""Unit tests for opendj MCP safety rails."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.opendj_cli.mcp_safety import (
    _DESTRUCTIVE_PATH_PATTERNS,
    SafetyRefusal,
    _is_recorder_lifecycle_path,
    agent_audio_mode,
    canonical_api_path,
    guard_library_request,
    guard_order,
    order_starts_audible_play,
    prepend_master_mute,
    validate_api_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
_OPENAPI = REPO_ROOT / "apps" / "webui" / "openapi.json"
_OPENAPI_PARAM_EXAMPLES = {
    "{session_id}": "2026-11-11T22-00-00",
    "{segment}": "0",
    "{idx}": "0",
    "{fmt}": "json",
}
_SETS_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DOCUMENTED_SETS_GET_QUERY_VARIANTS: tuple[tuple[str, str], ...] = (
    ("/api/sets?limit=1", "/api/sets"),
    ("/api/sets#frag", "/api/sets"),
    ("/api/sets/recorder?limit=1", "/api/sets/recorder"),
    ("/api/sets/recorder#frag", "/api/sets/recorder"),
)


def _concrete_openapi_sets_path(template: str) -> str:
    path = template
    for token, value in _OPENAPI_PARAM_EXAMPLES.items():
        path = path.replace(token, value)
    return path


def _documented_sets_operations() -> list[tuple[str, str]]:
    spec = json.loads(_OPENAPI.read_text(encoding="utf-8"))
    operations: list[tuple[str, str]] = []
    for template, methods in spec["paths"].items():
        if not template.startswith("/api/sets"):
            continue
        concrete = _concrete_openapi_sets_path(template)
        for method in methods:
            if method.startswith("x-"):
                continue
            operations.append((method.upper(), concrete))
    return operations


def _documented_sets_get_paths() -> list[str]:
    return [path for method, path in _documented_sets_operations() if method in {"GET", "HEAD"}]


def _documented_sets_mutation_operations() -> list[tuple[str, str]]:
    return [
        (method, path)
        for method, path in _documented_sets_operations()
        if method in _SETS_MUTATION_METHODS
    ]


def test_destructive_patterns_cover_delete_and_ingest() -> None:
    methods = {method for method, _ in _DESTRUCTIVE_PATH_PATTERNS}
    assert "DELETE" in methods
    assert "POST" in methods


@pytest.mark.requirement("LIBM-14825")
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/tracks/t-a:delete",
        "/api/v1/tracks/t-a%3Adelete",
        "/api/v1/tracks//t-a:delete",
        "/api/v1/tracks/t-a:delete?x=1",
    ],
)
def test_the_track_delete_custom_method_is_destructive(path: str) -> None:
    """[if] an agent POSTs /tracks/{id}:delete without the destructive switch [then] it is refused before dispatch, [else stop]."""
    with pytest.raises(SafetyRefusal) as error:
        guard_library_request("POST", path, environ={})
    assert error.value.code == "destructive_blocked"
    # Control: the destructive switch is what opens it, and the soft :remove stays open.
    guard_library_request("POST", path, environ={"OPENDJ_MCP_ENABLE_DESTRUCTIVE": "1"})
    guard_library_request("POST", "/api/v1/tracks/t-a:remove", environ={})


def test_guard_order_prepends_mute_for_play() -> None:
    order = {"single": {"type": "play", "deck": 1, "playing": True}}
    assert order_starts_audible_play(order)
    rewritten, safety = guard_order(order, environ={})
    assert safety == {"prepended_master_mute": True}
    assert "sequence" in rewritten
    assert rewritten["sequence"][0] == {"type": "master_mute", "muted": True, "persist": False}, (
        "if the safety mute is persistable then an agent run mutes every browser on its next load"
    )


@pytest.mark.requirement("AGENT-25")
def test_a_live_agent_session_never_mutes_a_play() -> None:
    """[if] OPENDJ_AGENT_AUDIO=live [then] a play order goes out unchanged, [else stop]."""
    order = {"single": {"type": "play", "deck": 1, "playing": True}}
    assert guard_order(order, environ={"OPENDJ_AGENT_AUDIO": "live"}) == (order, None)
    # Control: the same order in an explicitly muted session is still muted first.
    muted, safety = guard_order(order, environ={"OPENDJ_AGENT_AUDIO": "muted"})
    assert safety == {"prepended_master_mute": True}
    assert muted["sequence"][0]["type"] == "master_mute"


@pytest.mark.requirement("AGENT-25")
def test_every_audible_play_reapplies_the_mute_after_an_earlier_play_already_sent_one() -> None:
    """[if] an earlier play already muted the master in this muted session [then] the next play still prepends the mute, [else stop]."""
    order = {"single": {"type": "play", "deck": 1, "playing": True}}
    for _ in range(2):
        muted, safety = guard_order(order, environ={})
        assert safety == {"prepended_master_mute": True}
        assert muted["sequence"][0]["type"] == "master_mute"


@pytest.mark.requirement("AGENT-25")
def test_a_muted_session_refuses_an_order_that_unmutes_the_master() -> None:
    """[if] OPENDJ_AGENT_AUDIO is not live [then] an unmute is refused even with nothing to play, [else stop]."""
    unmute = {"single": {"type": "master_mute", "muted": False, "persist": False}}
    with pytest.raises(SafetyRefusal) as refused:
        guard_order(unmute, environ={})
    assert refused.value.code == "master_unmute_refused"
    assert guard_order(unmute, environ={"OPENDJ_AGENT_AUDIO": "live"}) == (unmute, None)
    # Control: a mute is not an unmute, so it goes out unchanged in a muted session.
    mute = {"single": {"type": "master_mute", "muted": True, "persist": False}}
    assert guard_order(mute, environ={}) == (mute, None)


@pytest.mark.requirement("AGENT-29")
def test_a_muted_session_refuses_a_raw_finish_now_order() -> None:
    """[if] a muted session sends a raw plan_amend finish_now on the command tool [then] it is refused as DjService.amend refuses it, [else stop]."""
    finish = {"plan_amend": {"id": "plan-x", "action": "finish_now"}}
    with pytest.raises(SafetyRefusal) as refused:
        guard_order(finish, environ={})
    assert refused.value.code == "amend_refused"
    assert guard_order(finish, environ={"OPENDJ_AGENT_AUDIO": "live"}) == (finish, None)
    # Control: an abort or an extend is not a finish, so a muted session may send it.
    abort = {"plan_amend": {"id": "plan-x", "action": "abort"}}
    assert guard_order(abort, environ={}) == (abort, None)


@pytest.mark.requirement("AGENT-25")
def test_agent_audio_defaults_to_muted_and_refuses_anything_else() -> None:
    """[if] OPENDJ_AGENT_AUDIO is unset [then] the session is muted, [else stop]."""
    assert agent_audio_mode({}) == "muted"
    assert agent_audio_mode({"OPENDJ_AGENT_AUDIO": " LIVE "}) == "live"
    with pytest.raises(SafetyRefusal) as refused:
        agent_audio_mode({"OPENDJ_AGENT_AUDIO": "on"})
    assert refused.value.code == "agent_audio_invalid"


def test_prepend_master_mute_wraps_single() -> None:
    order = {"single": {"type": "play", "deck": 1, "playing": True}}
    rewritten, prepended = prepend_master_mute(order)
    assert prepended is True
    assert rewritten["sequence"][0] == {"type": "master_mute", "muted": True, "persist": False}


@pytest.mark.requirement("AGENT-29")
@pytest.mark.parametrize("environ", [{}, {"OPENDJ_AGENT_AUDIO": "live"}])
def test_a_raw_plan_is_refused_in_every_audio_mode(environ: dict[str, str]) -> None:
    """[if] a raw plan reaches the command tool [then] it is refused, since only schedule_plan serializes its load, [else stop]."""
    plan = {"plan": {"prepare": [{"type": "seek", "deck": 2, "position_ms": 0}], "events": []}}
    with pytest.raises(SafetyRefusal) as refused:
        guard_order(plan, environ=environ)
    assert refused.value.code == "plan_use_schedule"
    # Control: an ordinary command is not caught by the plan refusal.
    assert guard_order({"single": {"type": "fader", "deck": 1, "value": 0.5}}, environ=environ)[0] == {
        "single": {"type": "fader", "deck": 1, "value": 0.5}
    }


def test_validate_api_path_rejects_traversal() -> None:
    with pytest.raises(SafetyRefusal) as error:
        validate_api_path("/api/v1/../health")
    assert error.value.code == "invalid_path"


@pytest.mark.parametrize(
    "path", ["/api/v1/%2e%2e/health", "/api/v1/%2E%2E/health", "%2Fapi/v1/tracks"]
)
def test_validate_api_path_checks_the_decoded_path(path: str) -> None:
    """[if] only the raw path is checked [then] %2e%2e walks out of /api/v1, [else stop]."""
    with pytest.raises(SafetyRefusal) as error:
        validate_api_path(path)
    assert error.value.code == "invalid_path"


@pytest.mark.requirement("SET-12315")
@pytest.mark.parametrize("method", ["GET", "HEAD", "get", "head"])
@pytest.mark.parametrize(
    ("path", "canonical"),
    [
        ("/api/sets", "/api/sets"),
        ("/api/sets/", "/api/sets/"),
        ("/api/sets?limit=5", "/api/sets"),
        ("/api/sets#top", "/api/sets"),
        ("/api/sets/recorder", "/api/sets/recorder"),
        ("/api/sets/session/timeline", "/api/sets/session/timeline"),
        ("/api/sets//%72ecorder", "/api/sets/recorder"),
    ],
)
def test_validate_sets_reads(method: str, path: str, canonical: str) -> None:
    """[if] sets is read explicitly [then] its canonical path is allowed, [else stop]."""
    assert validate_api_path(path, method=method) == canonical


@pytest.mark.requirement("SET-12315")
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    "path",
    [
        "/api/sets/../v1/health",
        "/api/sets/%2e%2e/v1/health",
        "/api/sets/%2E%2E/v1/health",
        "/api/sets/.%2e/v1/health",
        "/api/sets/%2e%2e%2fv1/health",
        "/api/sets/x/../recorder",
        "/api/sets/x/%2e%2e/recorder",
        "/api/sets-other",
        "/api/setsfoo/recorder",
        "/api/sets%2Frecorder",
        "/api/%73ets/recorder",
        "%2Fapi/sets/recorder",
        "http://localhost/api/sets/recorder",
        "/api/health",
    ],
)
def test_validate_sets_refuses_unsafe_paths(method: str, path: str) -> None:
    """[if] a sets read escapes its namespace [then] invalid_path is raised, [else stop]."""
    with pytest.raises(SafetyRefusal) as error:
        validate_api_path(path, method=method)
    assert error.value.code == "invalid_path"
    assert error.value.fields["path"] == path


@pytest.mark.requirement("SET-12315")
@pytest.mark.parametrize("method", [None, "PUT", "PATCH", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("path", ["/api/sets", "/api/sets/recorder/start"])
def test_validate_sets_refuses_writes_and_legacy_calls(method: str | None, path: str) -> None:
    """[if] sets has no read method [then] invalid_path is raised, [else stop]."""
    with pytest.raises(SafetyRefusal) as error:
        if method is None:
            validate_api_path(path)
        else:
            validate_api_path(path, method=method)
    assert error.value.code == "invalid_path"


@pytest.mark.requirement("SETREC-12874")
@pytest.mark.parametrize("method", ["POST", "post"])
@pytest.mark.parametrize("path", [
    "/api/sets/recorder/start",
    "/api/sets/recorder/2026-11-11T22-00-00/stop",
    "/api/sets/recorder/2026-11-11T22-00-00_2/recover",
])
def test_validate_recorder_lifecycle_writes(method: str, path: str) -> None:
    """[if] REC uses its lifecycle POST routes [then] the guard admits them, [else stop]."""
    assert validate_api_path(path, method=method) == path
    guard_library_request(method, path, environ={})


@pytest.mark.requirement("SETREC-12874")
@pytest.mark.parametrize("path", [
    "/api/sets",
    "/api/sets/recorder",
    "/api/sets/recorder/start/extra",
    "/api/sets/recorder/start/",
    "/api/sets/recorder/2026-11-11T22-00-00/master-pcm",
    "/api/sets/recorder/2026-11-11T22-00-00/stop/extra",
    "/api/sets/recorder/2026-11-11T22-00-00/stream-encoder",
    "/api/sets/session/transitions/0/label",
    "/api/setsfoo/recorder/start",
    "/api/sets%2frecorder/start",
    "/api/sets/recorder/x/../start",
    "/api/sets/recorder/x/%2e%2e/start",
    "/api/sets/recorder/2026-11-11T22-00-00%2fstop/recover",
    "/api/sets/recorder/2026-11-11T22-00-00%252fstop/recover",
    "/api/sets/recorder/invalid/stop",
])
def test_validate_recorder_rejects_other_writes(path: str) -> None:
    """[if] a POST is outside REC lifecycle [then] invalid_path is raised, [else stop]."""
    with pytest.raises(SafetyRefusal) as error:
        validate_api_path(path, method="POST")
    assert error.value.code == "invalid_path"


@pytest.mark.requirement("SET-12315")
@pytest.mark.parametrize("method", [None, "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_validate_v1_permissions_are_unchanged(method: str | None) -> None:
    """[if] a v1 path is validated [then] every existing method stays allowed, [else stop]."""
    assert validate_api_path("/api/v1/tracks", method=method) == "/api/v1/tracks"


@pytest.mark.requirement("SET-12315")
@pytest.mark.parametrize("path", _documented_sets_get_paths())
def test_documented_sets_get_routes_allowed(path: str) -> None:
    """[if] openapi lists a sets GET [then] validate_api_path admits it, [else stop]."""
    got = validate_api_path(path, method="GET")
    assert got.startswith("/api/sets")


@pytest.mark.requirement("SET-12315")
@pytest.mark.parametrize(("path", "canonical"), _DOCUMENTED_SETS_GET_QUERY_VARIANTS)
def test_documented_sets_get_query_or_fragment_allowed(path: str, canonical: str) -> None:
    """[if] a sets GET has query or fragment [then] the gate strips and allows, [else stop]."""
    assert validate_api_path(path, method="GET") == canonical


@pytest.mark.requirement("SET-12315")
@pytest.mark.parametrize(("method", "path"), _documented_sets_mutation_operations())
def test_documented_sets_mutations_refused(method: str, path: str) -> None:
    """[if] openapi lists a sets mutation [then] gate refuses unless REC lifecycle, [else stop]."""
    if method == "POST" and _is_recorder_lifecycle_path(path):
        assert validate_api_path(path, method=method) == path
        return
    with pytest.raises(SafetyRefusal) as error:
        validate_api_path(path, method=method)
    assert error.value.code == "invalid_path"


def test_guard_library_writeback() -> None:
    with pytest.raises(SafetyRefusal) as error:
        guard_library_request("POST", "/api/v1/playlists/x/writeback/apply")
    assert error.value.code == "writeback_blocked"


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("/api/v1/tracks", "/api/v1/tracks"),
        ("/api/v1/tracks/", "/api/v1/tracks/"),
        ("/api/v1/tracks?limit=5&q=a/../b", "/api/v1/tracks"),
        ("/api/v1/tracks#top", "/api/v1/tracks"),
        ("/api/v1/%74racks", "/api/v1/tracks"),
        ("/api/v1/a%3Fb", "/api/v1/a?b"),
        ("/api/v1//tracks", "/api/v1/tracks"),
        ("/api/v1/./tracks", "/api/v1/tracks"),
        ("/api/v1/x/../tracks", "/api/v1/tracks"),
        ("/api/v1/x/%2e%2e/tracks", "/api/v1/tracks"),
        ("/api/v1/%2561", "/api/v1/%61"),
        ("/api/v1/x/.", "/api/v1/x/"),
    ],
)
def test_canonical_api_path_is_the_route_the_engine_sees(raw: str, canonical: str) -> None:
    """[if] a rail reads anything else [then] encoding the path walks around that rail."""
    assert canonical_api_path(raw) == canonical


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/playlists/x/%77riteback/apply",
        "/api/v1/playlists/x%2Fwriteback%2Fapply",
        "/api/v1/playlists/x//writeback//apply",
        "/api/v1/playlists/x/./writeback/apply",
        "/api/v1/tracks/../playlists/x/writeback/apply",
    ],
)
def test_the_writeback_rail_reads_the_canonical_path(path: str) -> None:
    """[if] the marker is matched on the raw path [then] one encoded letter reaches rekordbox."""
    with pytest.raises(SafetyRefusal) as error:
        guard_library_request("POST", path)
    assert error.value.code == "writeback_blocked"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/%69ngest/run",
        "/api//v1/ingest/run",
        "/api/v1/tracks/../ingest/run",
        "/api/v1/playlists/p1/%74racks",
        "/api/v1/playlists//p1//tracks",
        "/api/v1/playlists/p1/tracks?position=0",
        "/api/v1/playlists/p1/tracks#x",
    ],
)
def test_the_destructive_rail_reads_the_canonical_path(path: str) -> None:
    """[if] a pattern is matched on the raw path [then] a query string alone opens the gate."""
    with pytest.raises(SafetyRefusal) as error:
        guard_library_request("POST", path, environ={})
    assert error.value.code == "destructive_blocked"
    # Control: the destructive gate is still the only thing refusing these.
    guard_library_request("POST", path, environ={"OPENDJ_MCP_ENABLE_DESTRUCTIVE": "1"})


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/playlists/p1/tracks?position=0"),
        ("POST", "/api/v1/playlists"),
        ("POST", "/api/v1/tracks/a%20b/tags"),
        ("PUT", "/api/v1/ui-prefs"),
    ],
)
def test_the_rails_still_pass_ordinary_calls(method: str, path: str) -> None:
    """[if] canonicalizing over-refuses [then] an ordinary library call stops working."""
    guard_library_request(method, path, environ={})
    assert validate_api_path(path).startswith("/api/v1/")


def test_a_refusal_named_error_keeps_its_code() -> None:
    """if the write guard's {"error": ...} refusal reaches a tool as error None then broken."""
    from apps.opendj_cli.mcp_safety import engine_error_code

    assert engine_error_code({"error": "locked_by_peer", "message": "x"}, 503) == "locked_by_peer"
    assert engine_error_code({"code": "no_library_db"}, 503) == "no_library_db"
    assert engine_error_code({"message": "no name"}, 503) == "http_503"
    assert engine_error_code("plain text", 500) == "http_500"
    assert engine_error_code(None, 404) == "http_404"


def test_a_sequence_re_mutes_immediately_before_each_audible_play() -> None:
    """The page awaits each sequence step, so an unmute during a slow load before a play must not let that play sound."""
    order = {
        "sequence": [
            {"type": "load", "deck": 1, "stable_id": "trk-a"},
            {"type": "play", "deck": 1, "playing": True},
        ]
    }
    rewritten, safety = guard_order(order, environ={})
    assert safety == {"prepended_master_mute": True}
    commands = rewritten["sequence"]
    play_at = next(i for i, command in enumerate(commands) if command.get("type") == "play")
    assert commands[play_at - 1] == {"type": "master_mute", "muted": True, "persist": False}, commands
    assert [c for c in commands if c.get("type") == "load"] == [{"type": "load", "deck": 1, "stable_id": "trk-a"}]
