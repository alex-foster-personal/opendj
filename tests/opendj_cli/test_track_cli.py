"""``opendj track key-segments <stable_id>``: the CLI parity surface for
nav1-key-record's `key_segments` block.

(private path) item 3: "prints the same
`key_segments` block (status, reason, segments) the `/anlz` payload carries,
with an end-to-end test that writes a two-segment record, runs the command,
and checks the printed block against the wire payload."

Never touches the AGENT-03 engine bus: no lock file, no running deck engine.

  [if] the CLI prints a different block than the wire overlay [then] broken,
  [else stop]

-Claude
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import uvicorn
from starlette.types import Message, Receive, Scope, Send

from apps.adapters.rekordbox import config as rb_config
from apps.analysis import selection
from apps.analysis.backends.own_key import record_from_estimate
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import open_conn, upsert_record
from apps.analysis_key import canon, flags, profiles, segments
from apps.analysis_key.lane_payload import depends_on_identity
from apps.opendj_cli import api_cli, track_cli
from apps.opendj_cli.__main__ import _parser, main
from apps.shared import platform_paths
from apps.shared.state import db as library_db
from apps.shared.state import library_tags
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.etag import compute_etag
from apps.webui.server.rb_vendor_pkg.own_key_overlay import (
    OWN_KEY_MISSING_REASON,
    apply_own_key_segments,
)
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.waits import start_uvicorn_in_thread, stop_uvicorn_in_thread

REPO_ROOT = Path(__file__).resolve().parents[2]
C_MAJOR = canon.Key(0, False)
A_MINOR = canon.Key(9, True)
FINGERPRINT_HEX = "3f" * 32
FINGERPRINT = "sha256:" + FINGERPRINT_HEX


@pytest.fixture(autouse=True)
def _reset_toggles() -> Any:
    selection.reset_toggles()
    yield
    selection.reset_toggles()


def _run_track_cli(
    argv: list[str], *, data_dir: Path, backend_port: int | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MDT_DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = str(REPO_ROOT)
    if backend_port is not None:
        env["MUSIC_DJ_BACKEND_PORT"] = str(backend_port)
        env["MUSIC_DJ_FRONTEND_PORT"] = str(backend_port + 1)
    return subprocess.run(
        [sys.executable, "-m", "apps.opendj_cli", *argv],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        check=False,
        timeout=60,
    )


def _bar_chroma(keys: list[canon.Key]) -> Any:
    import numpy as np

    columns = []
    for key in keys:
        third = 3 if key.is_minor else 4
        vector = np.zeros(12)
        for interval in (0, third, 7):
            vector[(key.pitch_class + interval) % 12] = 1.0
        columns.append(vector / np.linalg.norm(vector))
    return np.stack(columns, axis=1)


def _two_segment_block() -> dict[str, Any]:
    grid = segments.BarGrid(
        starts=tuple(float(bar) * 2.0 for bar in range(32)),
        ends=tuple(float(bar) * 2.0 for bar in range(1, 33)),
    )
    block = segments.segment_bars(
        _bar_chroma([C_MAJOR] * 16 + [A_MINOR] * 16), grid, duration_s=64.0
    )
    assert block.status == "ok" and len(block.segments) == 2, block
    return block.to_payload()


def _matching_beatgrid_record(stable_id: str) -> AnalysisRecord:
    from datetime import UTC, datetime

    beats = [
        {
            "t": round(index * 0.5, 5),
            "n": (index % 4) + 1,
            "bpm": 120.0,
        }
        for index in range(32)
    ]
    payload = {
        "beats": beats,
        "bpm": 120.0,
        "bpm_confidence": 0.9,
        "octave_reason": "in_band",
        "first_downbeat_s": 0.0,
        "tempo_changes": [],
        "static_grid_untrusted": False,
    }
    return AnalysisRecord(
        stable_id=stable_id,
        backend="own_beatgrid.backfill",
        backend_version="1.0.0",
        analyzed_at=datetime.now(UTC),
        duration_s=64.0,
        sample_rate=44100,
        bpm=120.0,
        bpm_confidence=0.9,
        key_camelot="",
        key_openkey="",
        key_confidence=0.0,
        energy=0,
        producer="backfill",
        producer_version="1.0.0",
        uses_model=False,
        model_sha256=None,
        decode_fingerprint=FINGERPRINT,
        lanes={"beatgrid": LaneResult(status="ok", payload=payload)},
    )


def _insert_track(state_db: Path, stable_id: str) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    conn = open_conn(state_db)
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (stable_id, "inferred", f"title-{stable_id}", now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_two_segment_record(state_db: Path, stable_id: str) -> None:
    _insert_track(state_db, stable_id)
    grid = _matching_beatgrid_record(stable_id)
    upsert_record(grid, db_path=state_db)
    depends_on = depends_on_identity(
        backend=grid.backend,
        producer_version=grid.producer_version,
        model_sha256=grid.model_sha256,
        decode_fingerprint=grid.decode_fingerprint,
        beatgrid_payload=grid.lanes["beatgrid"].payload,
    )
    estimate = profiles.KeyEstimate(key=C_MAJOR, confidence=0.9, margin=0.4)
    record = record_from_estimate(
        stable_id=stable_id,
        estimate=estimate,
        flag=flags.evaluate_tonal_center(estimate),
        duration_s=64.0,
        sample_rate=44100,
        decode_fingerprint=FINGERPRINT_HEX,
        segments_block=_two_segment_block(),
        depends_on_beatgrid=depends_on,
    )
    upsert_record(record, db_path=state_db)
    conn = open_conn(state_db)
    try:
        selection.set_default(conn, "key", "own")
    finally:
        conn.close()


def test_key_segments_prints_the_same_block_the_wire_payload_carries(
    state_db: Path,
) -> None:
    stable_id = "sid-cli-key-segments"
    _seed_two_segment_record(state_db, stable_id)
    data_dir = state_db.parent.parent

    done = _run_track_cli(
        ["track", "key-segments", stable_id, "--json"], data_dir=data_dir
    )
    printed = json.loads(done.stdout)

    assert done.returncode == 0
    wire_block = apply_own_key_segments(
        {}, stable_id, state_db_path=state_db
    )["key_segments"]
    assert printed == wire_block
    assert printed["status"] == "ok"
    assert len(printed["segments"]) == 2


def test_key_segments_with_no_own_record_reports_missing(state_db: Path) -> None:
    stable_id = "sid-no-record"
    _insert_track(state_db, stable_id)
    conn = open_conn(state_db)
    try:
        selection.set_default(conn, "key", "own")
    finally:
        conn.close()

    done = _run_track_cli(
        ["track", "key-segments", stable_id, "--json"],
        data_dir=state_db.parent.parent,
    )
    printed = json.loads(done.stdout)

    assert done.returncode == 0
    assert printed["status"] == "missing"
    assert printed["reason"] == OWN_KEY_MISSING_REASON
    assert printed["segments"] == []


def test_track_rejects_an_unknown_verb() -> None:
    done = _run_track_cli(["track"], data_dir=Path("/tmp"))
    assert done.returncode != 0


def test_key_segments_with_default_rbx_source_reports_missing(state_db: Path) -> None:
    _insert_track(state_db, "sid-rbx")
    done = _run_track_cli(
        ["track", "key-segments", "sid-rbx", "--json"],
        data_dir=state_db.parent.parent,
    )
    printed = json.loads(done.stdout)
    assert done.returncode == 0
    assert printed["status"] == "missing"
    assert printed["reason"] == "key lane source is rekordbox, not own"
    assert printed["segments"] == []


@pytest.mark.requirement("AGENT-05")
def test_key_segments_unknown_stable_id_is_not_found(state_db: Path) -> None:
    """[if] key-segments runs for an unknown stable_id [then] error.code is not_found, [else stop]."""
    done = _run_track_cli(
        ["track", "key-segments", "does-not-exist", "--json"],
        data_dir=state_db.parent.parent,
    )
    printed = json.loads(done.stdout)
    assert done.returncode != 0
    assert printed["error"]["code"] == "not_found"


def test_top_level_state_db_reaches_the_track_parser(state_db: Path) -> None:
    stable_id = "sid-cli-state-db"
    _seed_two_segment_record(state_db, stable_id)
    data_dir = state_db.parent.parent
    done = _run_track_cli(
        ["--state-db", str(state_db), "track", "key-segments", stable_id, "--json"],
        data_dir=data_dir,
    )
    printed = json.loads(done.stdout)
    assert done.returncode == 0
    assert printed["status"] == "ok"
    assert len(printed["segments"]) == 2


# ----- issue #3037: wrong default DB, missing DB reported as missing track -----


def test_explicit_state_db_that_does_not_exist_is_distinct_from_not_found(
    tmp_path: Path,
) -> None:
    """[if] --state-db names a file that is not there [then] error.code is
    state_db_not_found, naming that path, never not_found, [else stop]."""
    missing = tmp_path / "nonexistent" / "state.db"
    done = _run_track_cli(
        ["--state-db", str(missing), "track", "key-segments", "sid-x", "--json"],
        data_dir=tmp_path,
    )
    printed = json.loads(done.stdout)

    assert done.returncode != 0
    assert printed["error"]["code"] == "state_db_not_found"
    assert str(missing) in printed["error"]["message"]
    assert printed["error"]["code"] != "not_found"


def test_default_state_db_that_does_not_exist_is_also_distinct_from_not_found(
    tmp_path: Path,
) -> None:
    """The same distinction applies when the DB path is CLI-resolved, not
    user-named: MDT_DATA_DIR points at a real dir with no state/ subtree."""
    empty_data_dir = tmp_path / "data-dir-with-no-state-db"
    empty_data_dir.mkdir()
    done = _run_track_cli(
        ["track", "key-segments", "sid-x", "--json"], data_dir=empty_data_dir
    )
    printed = json.loads(done.stdout)

    assert done.returncode != 0
    assert printed["error"]["code"] == "state_db_not_found"
    assert str(empty_data_dir / "state" / "state.db") in printed["error"]["message"]


def test_resolve_state_db_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--state-db beats MDT_DATA_DIR beats the installed-app default."""
    monkeypatch.delenv("MDT_DATA_DIR", raising=False)
    monkeypatch.setenv(
        "OPENDJ_LIVE_LOCK_PATH", str(tmp_path / "app-default" / ".engine.lock")
    )

    # Neither override set: falls back to the app-default lock's directory.
    assert track_cli._resolve_state_db(None) == (
        tmp_path / "app-default" / "state" / "state.db"
    )

    # MDT_DATA_DIR set, no --state-db: MDT_DATA_DIR wins over the app default.
    monkeypatch.setenv("MDT_DATA_DIR", str(tmp_path / "mdt-data-dir"))
    assert track_cli._resolve_state_db(None) == (
        tmp_path / "mdt-data-dir" / "state" / "state.db"
    )

    # --state-db set: wins over both.
    explicit = tmp_path / "explicit" / "state.db"
    assert track_cli._resolve_state_db(explicit) == explicit


def test_default_state_db_never_resolves_inside_the_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Payload-layout regression test for issue #3037.

    Simulates the installed bundle's ``PROJECT_ROOT`` (``.../payload/app``)
    and proves the CLI's resolved default state DB is the app's OWN data
    dir -- read from the engine lock file's location, honoring
    OPENDJ_LIVE_LOCK_PATH exactly like every other opendj_cli command -- and
    never a path under that bundle-relative payload/app tree, without
    needing an actual macOS install to prove it.
    """
    monkeypatch.delenv("MDT_DATA_DIR", raising=False)

    fake_bundle_root = (
        tmp_path / "Open DJ.app" / "Contents" / "Resources" / "payload" / "app"
    )
    fake_bundle_root.mkdir(parents=True)
    fake_bundle_data_dir = fake_bundle_root / "data"
    monkeypatch.setattr(platform_paths, "PROJECT_ROOT", fake_bundle_root)
    monkeypatch.setattr(platform_paths, "DATA_DIR", fake_bundle_data_dir)
    monkeypatch.setattr(rb_config, "DATA_DIR", fake_bundle_data_dir)
    monkeypatch.setattr(
        rb_config, "STATE_DB", fake_bundle_data_dir / "state" / "state.db"
    )

    app_data_dir = tmp_path / "Application Support" / "com.opendj.desktop"
    monkeypatch.setenv("OPENDJ_LIVE_LOCK_PATH", str(app_data_dir / ".engine.lock"))

    resolved = track_cli._resolve_state_db(None)

    assert "payload" not in resolved.parts
    assert resolved != fake_bundle_data_dir / "state" / "state.db"
    assert resolved == app_data_dir / "state" / "state.db"


def test_every_usage_line_is_one_command() -> None:
    """If two usage entries run together on one line then broken."""
    from apps.opendj_cli import track_cli

    lines = [line for line in track_cli._USAGE.splitlines() if line.strip()]
    assert lines and all(line.count("opendj track") <= 1 for line in lines), lines


# ----- issue #12212: FindReplaceModal apply parity -----


@pytest.mark.requirement("TRACKEDIT-12698")
@pytest.mark.parametrize("field,mode,case_sensitive", [
    ("notes", "literal", False), ("genre", "literal", True), ("comments", "regex", False),
])
def test_find_replace_delegates_modal_payload(
    field: str, mode: str, case_sensitive: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] apply gets modal JSON [then] the same payload reaches the shared API, [else stop]."""
    payload = {
        "field": field, "stable_ids": ["second", "first"], "find": r"café\s+",
        "replace": "", "mode": mode, "case_sensitive": case_sensitive,
        "expected_etags": {"second": '"v2"', "first": '"v1"'},
    }
    request_json = json.dumps(payload, ensure_ascii=False)
    calls = []

    def delegate(rest: Sequence[str], *, as_json: bool) -> int:
        calls.append((list(rest), as_json))
        return api_cli.EXIT_CONFLICT

    monkeypatch.setattr(api_cli, "run", delegate)
    assert track_cli.run(["find-replace", "apply", request_json], as_json=False) == api_cli.EXIT_CONFLICT
    assert calls == [(["POST", "/api/v1/find-replace/apply", "--json", request_json], True)]


@pytest.mark.requirement("TRACKEDIT-12698")
@pytest.mark.parametrize("args", [[], ["apply"], ["preview", "{}"], ["apply", "{}", "extra"]])
def test_find_replace_bad_usage_makes_no_request(
    args: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] apply arguments are invalid [then] usage fails before any request, [else stop]."""
    def no_request(*args: object, **kwargs: object) -> int:
        raise AssertionError("invalid invocation reached the API")

    monkeypatch.setattr(api_cli, "run", no_request)
    assert main(["track", "find-replace", *args]) == api_cli.EXIT_FAILED
    assert "opendj track find-replace apply <request JSON>" in capsys.readouterr().err


@dataclass
class _ReplaceEngine:
    backend: SqliteBackend
    port: int
    responses: list[dict[str, Any]]

    def etag(self, stable_id: str) -> str:
        track = self.backend.get_track(stable_id)
        return compute_etag(track.stable_id, track.updated_at, track.selection_tag)

    def payload(self) -> dict[str, Any]:
        return {
            "field": "notes", "stable_ids": ["replace-first", "replace-noop"],
            "find": "warm", "replace": "hot", "mode": "literal", "case_sensitive": False,
            "expected_etags": {sid: self.etag(sid) for sid in ("replace-first", "replace-noop")},
        }

    def values(self) -> list[dict[str, Any]]:
        # New backend reads prove persistence rather than inspecting cached objects.
        backend = SqliteBackend(self.backend.writeback_state_db_path)
        return [backend.get_track(sid).to_dict() for sid in ("replace-first", "replace-noop")]


@pytest.fixture
def replace_engine(
    state_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_ReplaceEngine]:
    for sid in ("replace-first", "replace-noop"):
        _insert_track(state_db, sid)
    backend = SqliteBackend(state_db)
    engine = _ReplaceEngine(backend, 0, [])
    for sid, notes in (("replace-first", "Warm café"), ("replace-noop", "cool closer")):
        backend.update_track(sid, {"notes": notes, "genre": "House", "comments": "keep"},
                             expected_etag=engine.etag(sid))
    app = create_app(
        backend=backend, state_db_path=str(state_db), mount_frontend=False, enable_cors=False,
        client_error_log_dir=tmp_path / "client-errors", client_event_log_dir=tmp_path / "client-events",
    )
    app.state.data_dir = tmp_path

    async def observe(scope: Scope, receive: Receive, send: Send) -> None:
        response = bytearray()

        async def record(message: Message) -> None:
            if scope.get("path") == "/api/v1/find-replace/apply" and message["type"] == "http.response.body":
                response.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    engine.responses.append(json.loads(response))
            await send(message)

        await app(scope, receive, record)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        engine.port = int(listener.getsockname()[1])
        monkeypatch.setenv("MUSIC_DJ_BACKEND_PORT", str(engine.port))
        monkeypatch.setenv("MUSIC_DJ_FRONTEND_PORT", str(engine.port + 1))
        server, thread = start_uvicorn_in_thread(
            uvicorn.Config(observe, log_level="warning"), what="find-replace engine", sockets=[listener],
        )
        try:
            yield engine
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "find-replace engine failed to stop"


@pytest.mark.requirement("TRACKEDIT-12698")
@pytest.mark.parametrize("subprocess_entry", [False, True])
def test_find_replace_applies_through_production_route(
    replace_engine: _ReplaceEngine, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    subprocess_entry: bool,
) -> None:
    """[if] apply gets current ETags [then] real state and the route result agree, [else stop]."""
    argv = ["track", "find-replace", "apply", json.dumps(replace_engine.payload())]
    before = replace_engine.values()
    if subprocess_entry:
        done = _run_track_cli(["--json", *argv], data_dir=tmp_path, backend_port=replace_engine.port)
        code, out, err = done.returncode, done.stdout, done.stderr
    else:
        code = main(argv)
        captured = capsys.readouterr()
        out, err = captured.out, captured.err
    assert code == api_cli.EXIT_OK, err
    assert err == ""
    result = json.loads(out)
    assert result == replace_engine.responses[-1]
    assert result["applied_count"] == 1
    assert result["skipped_noop"] == ["replace-noop"]
    assert [(row["stable_id"], row["new_value"]) for row in result["results"]] == [("replace-first", "hot café")]
    after = replace_engine.values()
    assert after[0]["notes"] == "hot café"
    assert after[1] == before[1]
    assert [(row["genre"], row["comments"]) for row in after] == [("House", "keep")] * 2


@pytest.mark.requirement("TRACKEDIT-12698")
@pytest.mark.parametrize("refusal", ["stale-noop", "missing-etag", "unsupported-field"])
def test_find_replace_preserves_server_refusal(
    replace_engine: _ReplaceEngine, capsys: pytest.CaptureFixture[str], refusal: str,
) -> None:
    """[if] the server refuses apply [then] stderr preserves refusal and no row changes, [else stop]."""
    payload = replace_engine.payload()
    if refusal == "stale-noop":
        payload["expected_etags"]["replace-noop"] = '"stale"'
    elif refusal == "missing-etag":
        del payload["expected_etags"]["replace-noop"]
    elif refusal == "unsupported-field":
        payload["field"] = "title"
    before = replace_engine.values()
    code = main(["track", "find-replace", "apply", json.dumps(payload)])
    captured = capsys.readouterr()
    assert code == (api_cli.EXIT_CONFLICT if refusal == "stale-noop" else api_cli.EXIT_FAILED)
    assert captured.out == ""
    assert json.loads(captured.err) == replace_engine.responses[-1]
    assert replace_engine.values() == before


@pytest.mark.requirement("TRACKEDIT-12698")
def test_find_replace_preserves_zero_change_result(
    replace_engine: _ReplaceEngine, capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] nothing matches [then] the complete zero-change route result is printed, [else stop]."""
    payload = replace_engine.payload()
    payload["find"] = "absent"
    before = replace_engine.values()
    assert main(["track", "find-replace", "apply", json.dumps(payload)]) == api_cli.EXIT_OK
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == replace_engine.responses[-1] == {
        "applied_count": 0, "skipped_noop": payload["stable_ids"], "results": [],
    }
    assert replace_engine.values() == before


@pytest.mark.requirement("TRACKEDIT-12698")
def test_find_replace_malformed_json_makes_no_request(
    replace_engine: _ReplaceEngine, capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] request JSON is malformed [then] failure occurs without a route call, [else stop]."""
    assert main(["track", "find-replace", "apply", "{"]) == api_cli.EXIT_FAILED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not valid JSON" in captured.err
    assert replace_engine.responses == []


@pytest.mark.parametrize(
    ("argv", "method", "source", "body", "session", "as_json"),
    [
        (["track", "mytag-rename", "old", "new"], "PATCH", "old",
         {"name": "new", "merge": False}, "opendj-cli", False),
        (["--json", "track", "mytag-rename", "old", "new", "--merge",
          "--undo-session", "review"], "PATCH", "old",
         {"name": "new", "merge": True}, "review", True),
        (["track", "mytag-delete", "old", "--confirm", "--json"],
         "DELETE", "old", None, "opendj-cli", True),
        (["track", "mytag-delete", "--confirm", "--undo-session", "review", "--", "-old"],
         "DELETE", "-old", None, "review", False),
        (["track", "mytag-rename", "--", "-old", "-new"], "PATCH", "-old",
         {"name": "-new", "merge": False}, "opendj-cli", False),
        (["track", "mytag-rename", "café /#?%", "library", "--json"], "PATCH", "café /#?%",
         {"name": "library", "merge": False}, "opendj-cli", True),
        (["track", "mytag-delete", "mcp", "--confirm"],
         "DELETE", "mcp", None, "opendj-cli", False),
        (["track", "mytag-rename", "api", "dj"], "PATCH", "api",
         {"name": "dj", "merge": False}, "opendj-cli", False),
    ],
)
@pytest.mark.requirement("TRACKEDIT-12696")
def test_mytag_arguments_reach_the_existing_api(
    argv: list[str], method: str, source: str, body: dict[str, object] | None,
    session: str, as_json: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] a tag command is valid [then] its exact HTTP request is forwarded, [else stop]."""
    calls: list[list[str]] = []
    lock_path = tmp_path / "engine.lock"

    def capture(rest: list[str], *, as_json: bool, lock: Path | None) -> int:
        calls.append(rest)
        assert as_json == expected_json
        assert lock == lock_path
        return 9  # Dependency return value must survive the wrapper unchanged.

    expected_json = as_json
    monkeypatch.setattr(api_cli, "run", capture)
    assert main(["--lock", str(lock_path), *argv]) == 9
    assert len(calls) == 1
    tokens = calls[0]
    assert tokens[:2] == [method, f"/api/v1/mytags/{quote(source, safe='')}"]
    assert tokens[2:4] == ["-H", f"X-Undo-Session:{session}"]
    if body is None:
        assert len(tokens) == 4
    else:
        assert tokens[4] == "--json"
        assert json.loads(tokens[5]) == body
        assert len(tokens) == 6


@pytest.mark.parametrize(
    "rest",
    [
        ["mytag-delete", "old"],
        ["mytag-delete", "old", "--conf"],
        ["mytag-delete", "old", "--confirm=true"],
        ["mytag-delete", "old", "confirm"],
        ["mytag-delete", "--confirm"],
        ["mytag-delete", "old", "extra", "--confirm"],
        ["mytag-delete", "old", "--confirm", "--merge"],
        ["mytag-delete", "old", "--confirm", "--undo-session", ""],
        ["mytag-rename", "old"],
        ["mytag-rename", "old", "new", "extra"],
        ["mytag-rename", "old", "new", "--confirm"],
        ["mytag-rename", "old", "new", "--mer"],
        ["mytag-rename", "old", "new", "--undo-session", ""],
    ],
)
@pytest.mark.requirement("TRACKEDIT-12696")
def test_mytag_invalid_syntax_never_calls_http(
    rest: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] tag syntax lacks exact confirmation or arguments [then] no HTTP runs, [else stop]."""
    def unexpected(*args: object, **kwargs: object) -> int:
        pytest.fail("invalid syntax reached HTTP")

    monkeypatch.setattr(api_cli, "run", unexpected)
    assert main(["track", *rest]) != 0
    assert capsys.readouterr().err


@pytest.mark.requirement("TRACKEDIT-12696")
def test_mytag_help_and_list_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] tag commands are requested in help [then] both verbs are visible, [else stop]."""
    text = _parser().format_help()
    assert "mytag-rename" in text and "mytag-delete" in text and "--confirm" in text
    assert main(["--list-verbs", "track", "mytag-delete", "old"]) == 0
    assert "mytag-delete" in capsys.readouterr().out


@pytest.fixture
def mytag_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Path]]:
    """Disposable SQLite plus the production app and real loopback HTTP, no vendor data."""
    db_path = tmp_path / "state.db"
    conn = library_db.open_rw(db_path)
    writer = StateWriter(conn, actor="cli-test")
    try:
        for stable_id in ("one", "two"):
            writer.upsert_track(
                stable_id=stable_id, stable_id_tier="isrc", title=stable_id,
                artists=[], album=None, isrc=None, duration_ms=None, file_path=None,
            )
        library_tags.assign_tags(writer, "seed", ["one", "two"], add=["café /#?%"])
        library_tags.assign_tags(writer, "seed", ["two"], add=["existing"])
    finally:
        writer.close()
        conn.close()
    app = create_app(
        backend=SqliteBackend(db_path), state_db_path=str(db_path),
        mount_frontend=False, enable_cors=False,
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server, thread = start_uvicorn_in_thread(
            uvicorn.Config(app, log_level="warning"), what="MyTag CLI daemon", sockets=[listener],
        )
        monkeypatch.setattr(
            api_cli, "resolve_backend_base_url",
            lambda environ=None, lock_path=None: api_cli._BackendTarget(base_url=base_url),
        )
        try:
            yield base_url, db_path
        finally:
            stop_uvicorn_in_thread(server, thread, what="MyTag CLI daemon")


def _tag_memberships(base_url: str, db_path: Path) -> dict[str, list[str]]:
    response = httpx.get(f"{base_url}/api/v1/mytags/assignments", timeout=30)
    assert response.status_code == 200
    conn = library_db.open_ro(db_path)
    try:
        persisted = library_tags.track_tag_map(conn)
    finally:
        conn.close()
    assert response.json()["tracks"] == persisted
    return persisted


@pytest.mark.parametrize("session_args", [[], ["--undo-session", "review"]])
@pytest.mark.requirement("TRACKEDIT-12696")
def test_mytag_real_rename_delete_and_undo(
    mytag_daemon: tuple[str, Path], session_args: list[str], capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] tags are renamed or deleted through CLI [then] HTTP and undo agree, [else stop]."""
    base_url, db_path = mytag_daemon
    session = "review" if session_args else "opendj-cli"
    original = _tag_memberships(base_url, db_path)
    assert main(["track", "mytag-rename", "café /#?%", "Renamed", *session_args, "--json"]) == 0
    renamed = json.loads(capsys.readouterr().out)
    assert renamed["undo_id"]
    expected = {"one": ["Renamed"], "two": ["Renamed", "existing"]}
    assert _tag_memberships(base_url, db_path) == expected
    assert main(["track", "mytag-delete", "Renamed", "--confirm", *session_args]) == 0
    deleted = json.loads(capsys.readouterr().out)
    assert deleted["undo_id"] and deleted["row"]["tracks_updated"] == 2
    assert _tag_memberships(base_url, db_path) == {"two": ["existing"]}
    for operation, after, undo_id in (
        ("tag.delete", expected, deleted["undo_id"]),
        ("tag.rename", original, renamed["undo_id"]),
    ):
        undone = httpx.post(
            f"{base_url}/api/v1/undo", headers={"X-Undo-Session": session}, timeout=30,
        )
        assert undone.status_code == 200
        assert undone.json() == {"undo_id": undo_id, "op": operation}
        assert _tag_memberships(base_url, db_path) == after


@pytest.mark.parametrize("json_args", [[], ["--json"]])
@pytest.mark.requirement("TRACKEDIT-12696")
def test_mytag_real_refusals_and_explicit_merge(
    mytag_daemon: tuple[str, Path], json_args: list[str], capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] the server refuses a tag mutation [then] CLI preserves refusal and state, [else stop]."""
    base_url, db_path = mytag_daemon
    original = _tag_memberships(base_url, db_path)
    cases = [
        (["mytag-rename", "café /#?%", "existing"], "PATCH", "café /#?%",
         {"name": "existing", "merge": False}, api_cli.EXIT_CONFLICT),
        (["mytag-delete", "missing", "--confirm"], "DELETE", "missing", None, api_cli.EXIT_FAILED),
    ]
    for argv, method, source, body, exit_code in cases:
        response = httpx.request(
            method, f"{base_url}/api/v1/mytags/{quote(source, safe='')}", json=body,
            headers={"X-Undo-Session": "refusal"}, timeout=30,
        )
        assert response.status_code in (404, 409)
        assert main([*json_args, "track", *argv]) == exit_code
        printed = capsys.readouterr()
        assert not printed.out
        assert json.loads(printed.err) == response.json()
        assert _tag_memberships(base_url, db_path) == original
    assert main(["track", "mytag-rename", "café /#?%", "existing", "--merge"]) == 0
    assert json.loads(capsys.readouterr().out)["undo_id"]
    assert _tag_memberships(base_url, db_path) == {"one": ["existing"], "two": ["existing"]}
