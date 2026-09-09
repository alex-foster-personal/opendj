"""Per-lane source selection: persisted default, in-memory tri-state toggle.

[if] a relaunch loses a promoted default or the toggle is not unset [then] fail, [else stop].

Spec: `specs/native-analysis-v1.md` section 3 ("Effective source") and D1.
Requirement: PARITY-02.

Acceptance lines exercised here:
- [if] a relaunch happens [then] a promoted lane's default survives and the
  dev toggle is back to `unset`, its only launch state.
- [if] the toggle is set to rbx or own [then] it wins over the default; when
  it is `unset` it delegates.
- [if] an agent sets the source over HTTP [then] reading it back returns it.

-Claude
"""
from __future__ import annotations

import socket
import sqlite3
import subprocess
import sys

import pytest

from apps.analysis import selection
from apps.analysis.lanes import LANES

pytestmark = pytest.mark.requirement("PARITY-02")


#-----------------------------------------------------------------------------
# defaults and toggle
#-----------------------------------------------------------------------------

def test_every_lane_defaults_to_rbx_before_any_promotion(db) -> None:
    assert selection.all_defaults(db) == {lane: "rbx" for lane in LANES}


def test_default_survives_a_new_process(tmp_path) -> None:
    """The regression line: a relaunch must not undo a promotion.

    A real second process, not a re-import: the promotion has to be in the
    file, and an in-process check could pass on a module-level cache.
    """
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    selection.set_default(conn, "beatgrid", "own")
    conn.commit()
    conn.close()

    out = subprocess.run(
        [sys.executable, "-c",
         "import sqlite3, sys;"
         "from apps.analysis import selection;"
         "c = sqlite3.connect(sys.argv[1]);"
         "print(selection.get_default(c, 'beatgrid'), selection.get_toggle('beatgrid'))",
         str(db_path)],
        capture_output=True, text=True, check=True,
    )
    # The default persisted; the toggle came up unset in the fresh process.
    assert out.stdout.strip() == "own unset"


def test_toggle_launch_state_is_unset_for_every_lane() -> None:
    """A FRESH PROCESS, not importlib.reload.

    Reloading the module rebinds every class it defines, so any module that
    already imported `EffectiveField` (sqlite_backend does) would then be
    comparing two distinct classes -- an order-dependent failure in a
    neighbouring suite, observed Wed 9 Sep 2026. A subprocess also tests the
    thing the requirement is actually about, which is a relaunch.
    """
    out = subprocess.run(
        [sys.executable, "-c",
         "from apps.analysis import selection; print(sorted(selection.all_toggles().items()))"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == str(sorted((lane, "unset") for lane in LANES))


def test_toggle_overrides_the_default_in_both_directions(db) -> None:
    selection.set_default(db, "key", "own")
    assert selection.effective_source(db, "key") == "own"
    selection.set_toggle("key", "rbx")
    assert selection.effective_source(db, "key") == "rbx"
    selection.set_toggle("key", "own")
    assert selection.effective_source(db, "key") == "own"
    selection.set_toggle("key", "unset")
    assert selection.effective_source(db, "key") == "own"  # back to the default


def test_unknown_lane_source_and_toggle_state_are_refused(db) -> None:
    with pytest.raises(selection.SelectionError, match="unknown lane"):
        selection.get_default(db, "phrases")
    with pytest.raises(selection.SelectionError, match="unknown source"):
        selection.set_default(db, "key", "maybe")
    with pytest.raises(selection.SelectionError, match="unknown toggle state"):
        selection.set_toggle("key", "sometimes")


def test_get_default_works_on_a_connection_that_cannot_write(tmp_path) -> None:
    """The track read path is read-only; creating a table there would 500."""
    db_path = tmp_path / "state.db"
    sqlite3.connect(db_path).close()
    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        assert selection.get_default(ro, "beatgrid") == "rbx"
    finally:
        ro.close()


#-----------------------------------------------------------------------------
# HTTP endpoint (agent-native parity)
#-----------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    """A client whose SERVING backend is the database the endpoint writes.

    `create_app()` defaults to InMemoryBackend and the endpoint now refuses
    to persist a lane default on one, because a promotion the running
    backend cannot read would report `own` while /tracks kept serving
    rekordbox values (Codex P1).
    """
    from fastapi.testclient import TestClient

    from apps.analysis import store as analysis_store
    from apps.webui.server.app import create_app
    from apps.webui.server.sqlite_backend import SqliteBackend

    db_path = tmp_path / "state.db"
    analysis_store.open_conn(db_path).close()
    app = create_app()
    app.state.analysis_db_path = db_path
    app.state.backend = SqliteBackend(db_path)
    with TestClient(app) as c:
        yield c


def test_persisting_a_default_on_an_in_memory_backend_is_refused(tmp_path) -> None:
    """A promotion the running backend cannot serve is a lie, not a setting."""
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    app = create_app()
    app.state.analysis_db_path = tmp_path / "state.db"
    with TestClient(app) as client:
        resp = client.put(
            "/api/v1/analysis/source", json={"lane": "key", "default": "own"}
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "no_persistent_library"
        # The DEV TOGGLE stays available: it is process-local and resets on
        # relaunch, so it cannot mislead anyone past this session.
        assert client.put(
            "/api/v1/analysis/source", json={"lane": "key", "toggle": "own"}
        ).status_code == 200


def test_get_source_reports_every_lane(client) -> None:
    body = client.get("/api/v1/analysis/source").json()
    assert sorted(body["lanes"]) == sorted(LANES)
    assert body["lanes"]["beatgrid"] == {
        "default": "rbx", "toggle": "unset", "effective": "rbx",
    }


def test_every_toggle_state_is_settable_and_readable(client) -> None:
    for state in ("rbx", "own", "unset"):
        put = client.put(
            "/api/v1/analysis/source", json={"lane": "waveform", "toggle": state}
        )
        assert put.status_code == 200, put.text
        assert put.json()["lanes"]["waveform"]["toggle"] == state
        got = client.get("/api/v1/analysis/source").json()
        assert got["lanes"]["waveform"]["toggle"] == state


def test_setting_a_default_over_http_changes_the_effective_source(client) -> None:
    body = client.put(
        "/api/v1/analysis/source", json={"lane": "loudness", "default": "own"}
    ).json()
    assert body["lanes"]["loudness"] == {
        "default": "own", "toggle": "unset", "effective": "own",
    }


def test_put_with_neither_half_is_refused(client) -> None:
    resp = client.put("/api/v1/analysis/source", json={"lane": "key"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "no_change_requested"


def test_put_with_an_unknown_lane_is_refused(client) -> None:
    resp = client.put(
        "/api/v1/analysis/source", json={"lane": "phrases", "toggle": "own"}
    )
    assert resp.status_code == 422


def test_the_endpoint_is_in_the_committed_openapi() -> None:
    """Agent parity is a document, not just a route object."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    schema = json.loads((root / "apps/webui/openapi.json").read_text())
    assert "/api/v1/analysis/source" in schema["paths"]
    assert set(schema["paths"]["/api/v1/analysis/source"]) >= {"get", "put"}


#-----------------------------------------------------------------------------
# CLI: a client, never a second writer
#-----------------------------------------------------------------------------

def _a_port_nothing_is_listening_on() -> int:
    """Bind an ephemeral port, learn its number, release it.

    A real closed port on this host, so the CLI's connection genuinely
    fails rather than being told it did.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_cli_fails_loudly_and_names_the_url_when_the_service_is_down(
    monkeypatch, capsys,
) -> None:
    """The PRODUCTION resolver runs; only the environment it reads is set.

    `base_url` itself is not replaced. `MUSIC_DJ_BACKEND_PORT` is the first
    thing the real resolver consults and is what every recipe and the daemon
    already export, so setting it exercises the shipped path end to end:
    resolve, build the URL, attempt the connection, fail, report. Replacing
    `base_url` would have skipped the half of the CLI contract that says it
    must NAME the endpoint it tried.
    """
    from apps.analysis import selection_cli

    port = _a_port_nothing_is_listening_on()
    monkeypatch.setenv("MUSIC_DJ_BACKEND_PORT", str(port))

    # Control: the resolver really produced this URL, so the assertion below
    # is about the CLI's behavior and not about a string it was handed.
    assert selection_cli.base_url() == f"http://127.0.0.1:{port}/api/v1"

    code = selection_cli.main(["set-toggle", "key", "own"])
    out = capsys.readouterr().out
    assert code == selection_cli.EXIT_UNREACHABLE
    assert f"http://127.0.0.1:{port}/api/v1/analysis/source" in out
    # And it must NOT have mutated its own process state instead.
    assert selection.get_toggle("key") == "unset"


def test_cli_refuses_a_port_it_cannot_parse(monkeypatch, capsys) -> None:
    """Control: the resolver has to be able to FAIL, not just to return."""
    from apps.analysis import selection_cli

    monkeypatch.setenv("MUSIC_DJ_BACKEND_PORT", "not-a-port")
    assert selection_cli.main(["show"]) == selection_cli.EXIT_UNREACHABLE
    assert "not-a-port" in capsys.readouterr().out


def test_the_source_put_answers_to_the_cross_host_write_lock(tmp_path) -> None:
    """A mutating route must 503 when another host holds the writer lock.

    Exercised through the SAME `lock_status_fn` seam every other mutating
    route is tested through, not by patching the router: the claim is that
    this endpoint is subject to the repository's write exclusion, and the
    only honest way to show that is to trip the real one.
    """
    from fastapi.testclient import TestClient

    from apps.analysis import store as analysis_store
    from apps.webui.server.app import create_app
    from apps.webui.server.sqlite_backend import SqliteBackend

    db_path = tmp_path / "state.db"
    analysis_store.open_conn(db_path).close()
    app = create_app()
    app.state.analysis_db_path = db_path
    app.state.backend = SqliteBackend(db_path)
    app.state.hostname = "this-host"
    app.state.lock_status_fn = lambda: {"holder": "some-other-host"}

    with TestClient(app) as client:
        resp = client.put(
            "/api/v1/analysis/source", json={"lane": "key", "default": "own"}
        )
        assert resp.status_code == 503, resp.text

        # Control: with the lock free, the same request succeeds, so the 503
        # above is the guard and not a broken endpoint.
        app.state.lock_status_fn = lambda: {"holder": "this-host"}
        ok = client.put(
            "/api/v1/analysis/source", json={"lane": "key", "default": "own"}
        )
        assert ok.status_code == 200, ok.text
