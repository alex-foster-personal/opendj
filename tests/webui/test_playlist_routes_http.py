"""HTTP tests for playlist components and set routes (SET-6586).

[if] components are created, edited, listed or deleted over HTTP [then] the store's rules and refusals come back as 2xx, 404, 409 or 422, [else stop].
[if] a set is saved, re-routed or run with a live pick over HTTP [then] its parts and frozen run tracks follow, [else stop].
[if] a component or set is addressed under another playlist's path [then] 404, [else stop].
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.playlist_sets import load_run_entries
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("SET-6586")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    conn.close()
    return path


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    app = create_app(
        backend=SqliteBackend(db_path),
        state_db_path=str(db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c
    playlist_write.close_store(app)


def _playlist(client: TestClient, name: str) -> str:
    r = client.post("/api/v1/playlists", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["playlist_id"]


def _component(
    client: TestClient, pid: str, name: str, slot: str, tracks: list[str], tags: list[str] | None = None
) -> int:
    r = client.post(
        f"/api/v1/playlists/{pid}/components",
        json={"name": name, "slot": slot, "stable_ids": tracks, "tags": tags or []},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _two_endings(client: TestClient) -> tuple[str, int, int, int]:
    pid = _playlist(client, "Gig")
    opener = _component(client, pid, "Warm", "opener", ["a", "b"])
    ending_x = _component(client, pid, "Big", "ending", ["x"], ["peak"])
    ending_y = _component(client, pid, "Soft", "ending", ["y"], ["chill"])
    return pid, opener, ending_x, ending_y


def test_component_round_trip(client: TestClient) -> None:
    pid = _playlist(client, "Party")
    cid = _component(client, pid, "Warm", "opener", ["a", "b"], ["deep", "#deep"])
    listed = client.get(f"/api/v1/playlists/{pid}/components")
    assert listed.status_code == 200, listed.text
    [comp] = listed.json()["components"]
    assert comp["id"] == cid
    assert comp["stable_ids"] == ["a", "b"]
    assert comp["tags"] == ["deep"]

    renamed = client.patch(
        f"/api/v1/playlists/{pid}/components/{cid}", json={"name": "Warmer"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Warmer"
    assert renamed.json()["tags"] == ["deep"], "a field left out must not change"

    cleared = client.patch(f"/api/v1/playlists/{pid}/components/{cid}", json={"tags": []})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["tags"] == []

    gone = client.delete(f"/api/v1/playlists/{pid}/components/{cid}")
    assert gone.status_code == 204, gone.text
    assert client.get(f"/api/v1/playlists/{pid}/components").json()["components"] == []


def test_component_refusals_map_to_http(client: TestClient) -> None:
    pid = _playlist(client, "Party")
    cid = _component(client, pid, "Warm", "opener", ["a"])

    dup = client.post(
        f"/api/v1/playlists/{pid}/components",
        json={"name": "Warm", "slot": "ending", "stable_ids": ["z"]},
    )
    assert dup.status_code == 409, dup.text

    empty = client.post(
        f"/api/v1/playlists/{pid}/components",
        json={"name": "Blank", "slot": "opener", "stable_ids": []},
    )
    assert empty.status_code == 422, empty.text

    missing = client.patch(f"/api/v1/playlists/{pid}/components/99999", json={"name": "N"})
    assert missing.status_code == 404, missing.text

    created = client.post(f"/api/v1/playlists/{pid}/sets", json={"name": "S", "parts": [cid]})
    assert created.status_code == 201, created.text
    in_use = client.delete(f"/api/v1/playlists/{pid}/components/{cid}")
    assert in_use.status_code == 409, in_use.text
    assert in_use.json()["detail"]["set_ids"] == [created.json()["id"]]


def test_component_under_another_playlist_is_not_found(client: TestClient) -> None:
    pid = _playlist(client, "Mine")
    other = _playlist(client, "Other")
    cid = _component(client, pid, "Warm", "opener", ["a"])
    assert client.patch(
        f"/api/v1/playlists/{other}/components/{cid}", json={"name": "Stolen"}
    ).status_code == 404
    assert client.delete(f"/api/v1/playlists/{other}/components/{cid}").status_code == 404
    # The owner still sees it unchanged: the 404 came before any write.
    [comp] = client.get(f"/api/v1/playlists/{pid}/components").json()["components"]
    assert comp["name"] == "Warm"


def test_components_list_flags_a_borrowed_component(client: TestClient) -> None:
    pid, opener, ending_x, ending_y = _two_endings(client)
    other = _playlist(client, "Other gig")
    closer = _component(client, other, "Sig", "ending", ["zq-sig-1"])
    saved = client.post(
        f"/api/v1/playlists/{pid}/sets", json={"name": "Borrowed", "parts": [opener, closer]}
    )
    assert saved.status_code == 201, saved.text
    listed = client.get(f"/api/v1/playlists/{pid}/components").json()["components"]
    own = [c["id"] for c in listed if not c["borrowed"]]
    borrowed = [(c["id"], c["playlist_id"], c["stable_ids"]) for c in listed if c["borrowed"]]
    assert own == [opener, ending_x, ending_y]
    assert borrowed == [(closer, other, ["zq-sig-1"])]


def test_borrowed_slot_without_own_component_keeps_its_ladder_place(client: TestClient) -> None:
    pid, opener, ending_x, _ = _two_endings(client)
    other = _playlist(client, "Other gig")
    foreign = _component(client, other, "Opening", "opener", ["o1"])
    # Delete the own opener so the borrowed one is the only component in that slot.
    assert client.delete(f"/api/v1/playlists/{pid}/components/{opener}").status_code == 204
    saved = client.post(f"/api/v1/playlists/{pid}/sets", json={"name": "Open", "parts": [foreign, ending_x]})
    assert saved.status_code == 201, saved.text
    listed = client.get(f"/api/v1/playlists/{pid}/components").json()["components"]
    assert [(c["id"], c["borrowed"]) for c in listed][:2] == [(foreign, True), (ending_x, False)]


def test_borrowed_default_slot_the_playlist_never_owned_sorts_by_the_canonical_ladder(
    client: TestClient,
) -> None:
    pid = _playlist(client, "Gig")
    opener = _component(client, pid, "Warm", "opener", ["a"])
    main = _component(client, pid, "Main", "main", ["m"])
    other = _playlist(client, "Other gig")
    build = _component(client, other, "Lift", "build", ["b1"])
    saved = client.post(f"/api/v1/playlists/{pid}/sets", json={"name": "Lift", "parts": [opener, build, main]})
    assert saved.status_code == 201, saved.text
    listed = client.get(f"/api/v1/playlists/{pid}/components").json()["components"]
    assert [c["id"] for c in listed] == [opener, build, main]
    assert [c["borrowed"] for c in listed] == [False, True, False]


def test_editing_a_borrowed_component_publishes_the_borrowing_playlist(client: TestClient) -> None:
    from apps.shared import events as bus_seam

    pid, opener, _, _ = _two_endings(client)
    other = _playlist(client, "Other gig")
    closer = _component(client, other, "Sig", "ending", ["zq-sig-1"])
    assert client.post(f"/api/v1/playlists/{pid}/sets", json={"name": "B", "parts": [opener, closer]}).status_code == 201
    hub = _RecordingHub()
    bus_seam.set_hub(hub)
    try:
        assert client.patch(f"/api/v1/playlists/{other}/components/{closer}", json={"name": "Sig 2"}).status_code == 200
    finally:
        bus_seam.set_hub(None)
    ids = [payload["ids"] for _topic, payload in hub.seen if payload["kind"] == "playlist_components"]
    assert ids and pid in ids[-1] and other in ids[-1], ids
    # Control: the same edit with no borrowing set publishes only its own playlist.
    lone = _component(client, other, "Solo", "encore", ["zq-solo-1"])
    hub = _RecordingHub()
    bus_seam.set_hub(hub)
    try:
        assert client.patch(f"/api/v1/playlists/{other}/components/{lone}", json={"name": "Solo 2"}).status_code == 200
    finally:
        bus_seam.set_hub(None)
    assert [payload["ids"] for _t, payload in hub.seen][-1] == [other, str(lone)]


def test_routes_list_every_ending_and_mark_saved_sets(client: TestClient) -> None:
    pid, opener, ending_x, ending_y = _two_endings(client)
    saved = client.post(
        f"/api/v1/playlists/{pid}/sets", json={"name": "Peak night", "parts": [opener, ending_x]}
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["parts"] == [opener, ending_x]

    listing = client.get(f"/api/v1/playlists/{pid}/routes")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 2
    assert body["truncated"] is False
    by_parts = {tuple(r["parts"]): r for r in body["routes"]}
    assert by_parts[(opener, ending_x)]["set_id"] == saved.json()["id"]
    assert by_parts[(opener, ending_x)]["stable_ids"] == ["a", "b", "x"]
    assert by_parts[(opener, ending_y)]["set_id"] is None

    capped = client.get(f"/api/v1/playlists/{pid}/routes", params={"limit": 1}).json()
    assert len(capped["routes"]) == 1
    assert capped["total"] == 2
    assert capped["truncated"] is True

    assert client.get(f"/api/v1/playlists/{pid}/routes", params={"limit": 0}).status_code == 422


def test_route_rules_come_back_as_422(client: TestClient) -> None:
    pid, opener, ending_x, ending_y = _two_endings(client)
    two_endings = client.post(
        f"/api/v1/playlists/{pid}/sets", json={"name": "Both", "parts": [opener, ending_x, ending_y]}
    )
    assert two_endings.status_code == 422, two_endings.text
    backwards = client.post(
        f"/api/v1/playlists/{pid}/sets", json={"name": "Back", "parts": [ending_x, opener]}
    )
    assert backwards.status_code == 422, backwards.text
    first = client.post(f"/api/v1/playlists/{pid}/sets", json={"name": "One", "parts": [opener, ending_x]})
    assert first.status_code == 201, first.text
    again = client.post(f"/api/v1/playlists/{pid}/sets", json={"name": "Two", "parts": [opener, ending_x]})
    assert again.status_code == 422, again.text


def test_replace_route_keeps_old_runs(client: TestClient, db_path: Path) -> None:
    pid, opener, ending_x, ending_y = _two_endings(client)
    set_id = client.post(
        f"/api/v1/playlists/{pid}/sets", json={"name": "Night", "parts": [opener, ending_x]}
    ).json()["id"]
    run = client.post(f"/api/v1/playlists/{pid}/sets/{set_id}/runs", json={"kind": "performance"})
    assert run.status_code == 201, run.text

    moved = client.put(f"/api/v1/playlists/{pid}/sets/{set_id}/parts", json={"parts": [opener, ending_y]})
    assert moved.status_code == 200, moved.text
    assert moved.json()["parts"] == [opener, ending_y]

    detail = client.get(f"/api/v1/playlists/{pid}/sets/{set_id}").json()
    [old_run] = detail["runs"]
    conn = state_db.open_rw(db_path)
    try:
        assert [sid for sid, _ in load_run_entries(conn, old_run["id"])] == ["a", "b", "x"]
    finally:
        conn.close()

    empty = client.put(f"/api/v1/playlists/{pid}/sets/{set_id}/parts", json={"parts": []})
    assert empty.status_code == 422, empty.text
    other = _playlist(client, "Other")
    assert client.put(
        f"/api/v1/playlists/{other}/sets/{set_id}/parts", json={"parts": [opener]}
    ).status_code == 404


def test_live_pick_run_freezes_the_ending_played(client: TestClient, db_path: Path) -> None:
    pid, opener, ending_x, ending_y = _two_endings(client)
    set_id = client.post(
        f"/api/v1/playlists/{pid}/sets", json={"name": "Night", "parts": [opener, ending_x]}
    ).json()["id"]
    run = client.post(
        f"/api/v1/playlists/{pid}/sets/{set_id}/runs",
        json={"kind": "performance", "played_parts": [opener, ending_y]},
    )
    assert run.status_code == 201, run.text
    assert run.json()["play_count"] == 1

    detail = client.get(f"/api/v1/playlists/{pid}/sets/{set_id}").json()
    assert detail["parts"] == [opener, ending_x], "a live pick must not rewrite the saved route"
    [played] = detail["runs"]
    conn = state_db.open_rw(db_path)
    try:
        assert [sid for sid, _ in load_run_entries(conn, played["id"])] == ["a", "b", "y"]
    finally:
        conn.close()

    off_route = client.post(
        f"/api/v1/playlists/{pid}/sets/{set_id}/runs",
        json={"kind": "performance", "played_parts": [ending_y]},
    )
    assert off_route.status_code == 422, off_route.text
    assert client.get(f"/api/v1/playlists/{pid}/sets/{set_id}").json()["play_count"] == 1


def test_openapi_operation_ids() -> None:
    paths = create_app(mount_frontend=False).openapi()["paths"]
    comps = paths["/api/v1/playlists/{playlist_id}/components"]
    assert comps["get"]["operationId"] == "list_playlist_components"
    assert comps["post"]["operationId"] == "create_playlist_component"
    one = paths["/api/v1/playlists/{playlist_id}/components/{component_id}"]
    assert one["patch"]["operationId"] == "update_playlist_component"
    assert one["delete"]["operationId"] == "delete_playlist_component"
    assert paths["/api/v1/playlists/{playlist_id}/routes"]["get"]["operationId"] == "list_playlist_routes"
    parts = paths["/api/v1/playlists/{playlist_id}/sets/{set_id}/parts"]
    assert parts["put"]["operationId"] == "update_playlist_set_parts"


class _RecordingHub:
    """A real EventHub for the seam in apps.shared.events: publish() runs unchanged."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict]] = []

    def publish(self, topic: str, payload: dict) -> None:
        self.seen.append((topic, payload))


def test_component_events_use_a_kind_the_frontend_bus_accepts(client: TestClient) -> None:
    import re

    from apps.shared import events as bus_seam

    bus = Path(__file__).resolve().parents[2] / "apps/webui/frontend/src/lib/api/events-bus.ts"
    block = re.search(r"LIBRARY_KINDS = \[(.*?)\] as const", bus.read_text(encoding="utf-8"), re.S)
    assert block is not None, "LIBRARY_KINDS not found in events-bus.ts"
    kinds = set(re.findall(r"'([a-z_]+)'", block.group(1)))
    assert "playlist_sets" in kinds, "control: the parse must find a kind known to be there"
    hub = _RecordingHub()
    bus_seam.set_hub(hub)
    try:
        pid = _playlist(client, "Bus")
        _component(client, pid, "Warm", "opener", ["a"])
    finally:
        bus_seam.set_hub(None)
    seen = [payload for _topic, payload in hub.seen]
    assert seen, "creating a component must publish through the real seam"
    assert all(p["kind"] in kinds for p in seen), seen


def test_component_write_refreshes_open_set_tabs_through_the_event_seam(client: TestClient) -> None:
    """A component write changes what a saved set resolves to, so the set views must hear about it.

    Goes through the real EventHub seam, not a stub of publish(): the kinds that come out are the ones the
    frontend bus subscribes to, and the set-tab refresh is published for the component's playlist.
    """
    from apps.shared import events as bus_seam

    pid = _playlist(client, "Gig")
    cid = _component(client, pid, "Warm", "opener", ["a", "b"])
    hub = _RecordingHub()
    bus_seam.set_hub(hub)
    try:
        r = client.patch(f"/api/v1/playlists/{pid}/components/{cid}", json={"tags": ["warm"]})
    finally:
        bus_seam.set_hub(None)
    assert r.status_code == 200, r.text
    seen = {payload["kind"]: payload["ids"] for _topic, payload in hub.seen}
    assert seen.get("playlist_sets") == [pid, str(cid)]
    assert seen.get("playlist_components") == [pid, str(cid)]


def test_editing_a_component_refreshes_the_set_that_borrows_it(client: TestClient, db_path: Path) -> None:
    """A set in one playlist can borrow a component of another; editing it must refresh that set's tab."""
    from apps.shared import events as bus_seam
    from apps.shared.playlist_sets import create_playlist_set

    owner = _playlist(client, "Owner")
    borrower = _playlist(client, "Borrower")
    cid = _component(client, owner, "Closer", "ending", ["zq-1"])
    conn = state_db.open_rw(db_path)
    try:
        create_playlist_set(conn, borrower, "Borrowed", parts=[cid])
    finally:
        conn.close()
    hub = _RecordingHub()
    bus_seam.set_hub(hub)
    try:
        r = client.patch(f"/api/v1/playlists/{owner}/components/{cid}", json={"tags": ["late"]})
    finally:
        bus_seam.set_hub(None)
    assert r.status_code == 200, r.text
    sets = [payload["ids"] for _topic, payload in hub.seen if payload["kind"] == "playlist_sets"]
    assert sets and borrower in sets[0], sets
