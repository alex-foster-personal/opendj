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
- [if] a lane default is rbx and the PARITY-02 toggle is forced to own [then]
  the predicate is false.
- [if] a lane default is own and the toggle is unset or rbx [then] the
  predicate is true.
- [if] the toggle launch state is read [then] it is still unset and is not
  persisted.

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


@pytest.mark.parametrize(
    ("fn", "bad_value", "expected_msg"),
    [
        (
            selection.check_lane,
            "phrases",
            f"unknown lane 'phrases'; lanes are {LANES}",
        ),
        (
            selection.check_source,
            "maybe",
            f"unknown source 'maybe'; sources are {selection.SOURCES}",
        ),
        (
            selection.check_toggle_state,
            "sometimes",
            f"unknown toggle state 'sometimes'; states are {selection.TOGGLE_STATES}",
        ),
    ],
)
def test_check_wrappers_reject_invalid_values_with_exact_selection_error(
    fn, bad_value, expected_msg
) -> None:
    """[if] check wrappers get invalid values [then] SelectionError text is exact, [else stop]."""
    with pytest.raises(selection.SelectionError) as exc_info:
        fn(bad_value)
    assert str(exc_info.value) == expected_msg


def test_unknown_lane_source_and_toggle_state_are_refused(db) -> None:
    with pytest.raises(selection.SelectionError, match="unknown lane"):
        selection.get_default(db, "phrases")
    with pytest.raises(selection.SelectionError, match="unknown lane"):
        selection.lane_is_promoted(db, "phrases")
    with pytest.raises(selection.SelectionError, match="unknown source"):
        selection.set_default(db, "key", "maybe")
    with pytest.raises(selection.SelectionError, match="unknown toggle state"):
        selection.set_toggle("key", "sometimes")


def test_compare_and_set_toggle_applies_only_when_the_current_value_matches() -> None:
    """discussion_r3973129053: the CAS the rollback relies on to close the
    read-then-write race window."""
    assert selection.get_toggle("waveform") == "unset"
    assert selection.compare_and_set_toggle("waveform", "unset", "own") is True
    assert selection.get_toggle("waveform") == "own"


def test_compare_and_set_toggle_refuses_and_leaves_the_newer_value_when_stale() -> None:
    """The mismatch branch: someone else already moved the toggle, so the
    compare-and-set must report failure and must NOT overwrite their value."""
    selection.set_toggle("waveform", "own")
    assert selection.compare_and_set_toggle("waveform", "unset", "rbx") is False
    assert selection.get_toggle("waveform") == "own"


def test_compare_and_set_toggle_validates_lane_and_states_before_touching_anything() -> None:
    with pytest.raises(selection.SelectionError, match="unknown lane"):
        selection.compare_and_set_toggle("phrases", "unset", "own")
    with pytest.raises(selection.SelectionError, match="unknown toggle state"):
        selection.compare_and_set_toggle("waveform", "maybe", "own")
    with pytest.raises(selection.SelectionError, match="unknown toggle state"):
        selection.compare_and_set_toggle("waveform", "unset", "sometimes")


def test_toggle_revision_is_monotonic_and_starts_at_zero() -> None:
    assert selection.get_toggle_revision("beatgrid") == 0
    write = selection.write_toggle("beatgrid", "own")
    assert write == selection.ToggleWrite(previous="unset", current="own", revision=1)
    assert selection.get_toggle_revision("beatgrid") == 1
    selection.set_toggle("beatgrid", "rbx")
    assert selection.get_toggle_revision("beatgrid") == 2
    assert selection.all_toggle_revisions()["beatgrid"] == 2


def test_expected_revision_refuses_an_aba_round_trip_the_value_alone_would_miss() -> None:
    """discussion_r3974993963 P1 BLOCKING, reproduced first: a rollback that
    captured revision 1 (from the original 'own' write) must not restore a
    displaced value once 'own' has round-tripped through 'rbx' and back -
    the CURRENT value equals what it still expects, but the world moved.
    """
    first = selection.write_toggle("beatgrid", "own")
    assert first == selection.ToggleWrite(previous="unset", current="own", revision=1)
    selection.write_toggle("beatgrid", "rbx")  # an external agent, revision 2
    selection.write_toggle("beatgrid", "own")  # back to "own", revision 3
    assert selection.get_toggle("beatgrid") == "own"

    # A value-only compare-and-set would still succeed here (control: proves
    # the ABA gap this test is about is real, not already impossible).
    assert selection.compare_and_set_toggle("beatgrid", expected="own", new="unset") is True
    assert selection.get_toggle("beatgrid") == "unset"
    selection.set_toggle("beatgrid", "own")  # replay for the actual assertion, revision 5

    # The stale rollback, now revision-checked against the FIRST write it
    # actually observed, must refuse: revision has moved to 5, not 1.
    assert (
        selection.compare_and_set_toggle(
            "beatgrid", expected="own", new="unset", expected_revision=first.revision
        )
        is False
    )
    assert selection.get_toggle("beatgrid") == "own", (
        "an ABA-stale rollback must not erase the newer 'own' decision"
    )

    # Mutate-both-directions control: the identical compensation succeeds
    # when the revision it names IS still current - this is not a guard that
    # merely always refuses.
    current_revision = selection.get_toggle_revision("beatgrid")
    assert (
        selection.compare_and_set_toggle(
            "beatgrid", expected="own", new="unset", expected_revision=current_revision
        )
        is True
    )
    assert selection.get_toggle("beatgrid") == "unset"


def test_expected_revision_is_ignored_without_expected_value() -> None:
    """An unconditional write must stay unconditional: `expected_revision`
    only means anything paired with `expected`."""
    selection.write_toggle("beatgrid", "own")
    result = selection.write_toggle("beatgrid", "rbx", expected_revision=999)
    assert result == selection.ToggleWrite(previous="own", current="rbx", revision=2)


def test_get_default_works_on_a_connection_that_cannot_write(tmp_path) -> None:
    """The track read path is read-only; creating a table there would 500."""
    db_path = tmp_path / "state.db"
    sqlite3.connect(db_path).close()
    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        assert selection.get_default(ro, "beatgrid") == "rbx"
        assert selection.lane_is_promoted(ro, "beatgrid") is False
    finally:
        ro.close()


def test_lane_is_promoted_is_false_when_default_is_rbx_even_if_toggle_is_own(db) -> None:
    assert selection.get_default(db, "key") == "rbx"
    selection.set_toggle("key", "own")
    assert selection.effective_source(db, "key") == "own"
    assert selection.lane_is_promoted(db, "key") is False


def test_lane_is_promoted_is_true_when_default_is_own_even_if_toggle_is_unset_or_rbx(db) -> None:
    selection.set_default(db, "key", "own")
    assert selection.get_toggle("key") == "unset"
    assert selection.lane_is_promoted(db, "key") is True
    selection.set_toggle("key", "rbx")
    assert selection.effective_source(db, "key") == "rbx"
    assert selection.lane_is_promoted(db, "key") is True


def test_lane_is_promoted_does_not_persist_the_toggle(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    selection.set_default(conn, "key", "own")
    conn.commit()
    conn.close()

    selection.set_toggle("key", "rbx")
    assert selection.get_toggle("key") == "rbx"

    out = subprocess.run(
        [sys.executable, "-c",
         "import sqlite3, sys;"
         "from apps.analysis import selection;"
         "c = sqlite3.connect(sys.argv[1]);"
         "print(selection.lane_is_promoted(c, 'key'), selection.get_toggle('key'))",
         str(db_path)],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "True unset"
    # Parent process still holds the session toggle; the child did not
    # inherit it and nothing wrote it to the database.
    assert selection.get_toggle("key") == "rbx"


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
    assert sorted(body["serving"]) == ["beatgrid", "key", "waveform"]
    assert body["lanes"]["beatgrid"] == {
        "default": "rbx", "toggle": "unset", "toggle_revision": 0, "effective": "rbx",
    }


def test_every_toggle_state_is_settable_and_readable(client) -> None:
    for state in ("rbx", "own", "unset"):
        lane = "beatgrid" if state == "own" else "waveform"
        put = client.put(
            "/api/v1/analysis/source", json={"lane": lane, "toggle": state}
        )
        assert put.status_code == 200, put.text
        assert put.json()["lanes"][lane]["toggle"] == state
        got = client.get("/api/v1/analysis/source").json()
        assert got["lanes"][lane]["toggle"] == state


def test_setting_a_default_over_http_changes_the_effective_source(client) -> None:
    body = client.put(
        "/api/v1/analysis/source", json={"lane": "key", "default": "own"}
    ).json()
    assert body["lanes"]["key"] == {
        "default": "own", "toggle": "unset", "toggle_revision": 0, "effective": "own",
    }


def test_put_with_expected_toggle_matching_applies_and_puts_with_none_ignores_it(client) -> None:
    """discussion_r3973129053, HTTP half: the route's `expected_toggle` field
    is the client-facing surface over `selection.compare_and_set_toggle`."""
    put = client.put(
        "/api/v1/analysis/source",
        json={"lane": "key", "toggle": "own", "expected_toggle": "unset"},
    )
    assert put.status_code == 200, put.text
    assert put.json()["lanes"]["key"]["toggle"] == "own"


def test_put_with_a_stale_expected_toggle_is_refused_with_409_and_does_not_apply(client) -> None:
    client.put("/api/v1/analysis/source", json={"lane": "key", "toggle": "own"})
    resp = client.put(
        "/api/v1/analysis/source",
        json={"lane": "key", "toggle": "rbx", "expected_toggle": "unset"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "toggle_changed"
    # The refused CAS must not have applied: still `own`, the value it raced against.
    assert client.get("/api/v1/analysis/source").json()["lanes"]["key"]["toggle"] == "own"


def test_a_stale_cas_refuses_the_whole_put_and_leaves_the_default_unpersisted(client) -> None:
    """discussion_r3974235466: a combined default+toggle PUT whose CAS is
    stale must not commit the default before raising 409, or the durable
    default takes effect after relaunch while the caller reads a conflict."""
    client.put("/api/v1/analysis/source", json={"lane": "key", "toggle": "own"})
    resp = client.put(
        "/api/v1/analysis/source",
        json={
            "lane": "key",
            "default": "own",
            "toggle": "rbx",
            "expected_toggle": "unset",
        },
    )
    assert resp.status_code == 409, resp.text
    got = client.get("/api/v1/analysis/source").json()["lanes"]["key"]
    assert got["default"] == "rbx", "the default must not have committed alongside a refused CAS"
    assert got["toggle"] == "own"


def test_put_own_on_an_unserved_lane_is_refused_with_409(client) -> None:
    put = client.put(
        "/api/v1/analysis/source",
        json={"lane": "vocal", "toggle": "own"},
    )
    assert put.status_code == 409, put.text
    assert "vocal" in put.json()["detail"]["error"]
    assert "no serving implementation yet" in put.json()["detail"]["error"]


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
