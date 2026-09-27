"""Single-pin archive and follow-on (#858), the two buttons a done pin carries.

The bulk ``POST /feedback/archive`` empties the board; these move one completed
pin or create a follow-on issue while preserving the parent.

[if] Archive is pressed on a fixed/merged pin [then] it leaves comments.json and
    lands in an archive file with its status, issue_url and agent_note intact
[if] a second pin is archived [then] it joins the SAME archive file rather than
    scattering one file per press
[if] Follow-on is pressed [then] a new open pin exists at the same anchor whose
    text names the parent, and the parent is untouched
[if] Follow-on is pressed on an open/issued parent [then] 409, no child written
[if] the id is unknown [then] 404, nothing written
[if] the bulk archive runs [then] each comment keeps its status rather than
    being recorded as a bare pin
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    from apps.webui.server.app import create_app

    app = create_app(mount_frontend=False)
    app.state.data_dir = tmp_path
    return TestClient(app)


def _create(client: TestClient, text: str = "hot cues ignore BSM") -> dict:
    r = client.post(
        "/api/v1/feedback/comments",
        json={
            "x_pct": 10,
            "y_pct": 20,
            "anchor": ".bank",
            "page": "/performance",
            "text": text,
            "ui": "chrome-loop",
            "viewport_width": 1280,
            "viewport_height": 800,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _comments(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / "feedback" / "comments.json").read_text())["comments"]


def _archives(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "feedback").glob("archive-*.json"))


# ----- single-pin archive -------------------------------------------------
def test_archive_moves_one_pin_with_its_full_history(client: TestClient, tmp_path: Path) -> None:
    keep = _create(client, "still open")
    pin = _create(client, "artwork header icon is wrong")
    client.patch(
        f"/api/v1/feedback/comments/{pin['id']}",
        json={
            "status": "fixed",
            "issue_url": "https://github.com/maintainer/music-dj-tools/issues/888",
            "agent_note": "Fixed in 0980e915 on branch af--pin-review-2sep.",
        },
    )

    r = client.post(f"/api/v1/feedback/comments/{pin['id']}/archive")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["comment"]["id"] == pin["id"]
    assert out["comment"]["status"] == "archived", "the archived copy must say so"

    left = _comments(tmp_path)
    assert [c["id"] for c in left] == [keep["id"]], "only the archived pin leaves the canvas"

    files = _archives(tmp_path)
    assert len(files) == 1 and str(files[0]) == out["archived_to"]
    archived = json.loads(files[0].read_text())["comments"]
    assert len(archived) == 1
    assert archived[0]["text"] == "artwork header icon is wrong"
    assert archived[0]["issue_url"].endswith("/issues/888")
    assert archived[0]["agent_note"].startswith("Fixed in 0980e915")
    assert archived[0]["created_at"] == pin["created_at"], "history, not a fresh record"


def test_a_second_archive_joins_the_newest_file_rather_than_making_another(
    client: TestClient, tmp_path: Path
) -> None:
    first = _create(client, "one")
    second = _create(client, "two")
    for pin in (first, second):
        client.patch(f"/api/v1/feedback/comments/{pin['id']}", json={"status": "merged"})

    a = client.post(f"/api/v1/feedback/comments/{first['id']}/archive").json()
    b = client.post(f"/api/v1/feedback/comments/{second['id']}/archive").json()

    assert a["archived_to"] == b["archived_to"], (
        "one file per press would scatter the archive across dozens of files - broken"
    )
    assert len(_archives(tmp_path)) == 1
    archived = json.loads(_archives(tmp_path)[0].read_text())["comments"]
    assert [c["text"] for c in archived] == ["one", "two"]
    assert _comments(tmp_path) == []


def test_archiving_an_unknown_pin_is_404_and_writes_nothing(
    client: TestClient, tmp_path: Path
) -> None:
    _create(client)
    before = (tmp_path / "feedback" / "comments.json").read_text()
    r = client.post("/api/v1/feedback/comments/nope/archive")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "COMMENT_NOT_FOUND"
    assert (tmp_path / "feedback" / "comments.json").read_text() == before
    assert _archives(tmp_path) == [], "a 404 must not leave an empty archive file behind"


@pytest.mark.parametrize("status", ["open", "issued", "blocked"])
def test_archiving_an_unfinished_pin_is_rejected(
    client: TestClient, tmp_path: Path, status: str
) -> None:
    # This endpoint is agent-facing (AGENTS.md), so the "only done work is
    # archived" invariant has to hold here even though the widget already
    # hides the button before fixed/merged.
    pin = _create(client)
    if status != "open":
        client.patch(f"/api/v1/feedback/comments/{pin['id']}", json={"status": status})
    before = (tmp_path / "feedback" / "comments.json").read_text()

    r = client.post(f"/api/v1/feedback/comments/{pin['id']}/archive")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "PIN_NOT_DONE"
    assert (tmp_path / "feedback" / "comments.json").read_text() == before, (
        "a rejected archive must leave the pin exactly where it was"
    )
    assert _archives(tmp_path) == [], "a rejected archive must not create an archive file"


def test_patch_cannot_set_archived_directly(client: TestClient, tmp_path: Path) -> None:
    # `archived` may only be reached through POST .../archive, which also
    # writes the archive-file transaction. A bare PATCH must not be able to
    # make a pin vanish from comments.json with no archive-file entry.
    pin = _create(client)
    r = client.patch(f"/api/v1/feedback/comments/{pin['id']}", json={"status": "archived"})
    assert r.status_code == 422
    assert _comments(tmp_path)[0]["status"] in (None, "open")
    assert _archives(tmp_path) == []


def test_patch_rejects_an_explicit_null_text(client: TestClient, tmp_path: Path) -> None:
    # An explicit {"text": null} would otherwise pass CommentUpdateIn and then
    # fail CommentOut.model_validate() (text is required, non-None) with a
    # 500. It must be rejected as a 422 instead; omitting the field is the
    # correct way to leave text unchanged (covered elsewhere by every other
    # PATCH test in this file).
    pin = _create(client, "original text")
    r = client.patch(f"/api/v1/feedback/comments/{pin['id']}", json={"text": None})
    assert r.status_code == 422
    assert _comments(tmp_path)[0]["text"] == "original text"
    text_schema = client.get("/openapi.json").json()["components"]["schemas"]["CommentUpdateIn"][
        "properties"
    ]["text"]
    assert "anyOf" not in text_schema, (
        "if text accepts null in OpenAPI then typed clients can send a request "
        "the server cannot store - broken"
    )


def test_concurrent_archive_of_different_pins_loses_nothing(
    client: TestClient, tmp_path: Path
) -> None:
    # Two agents archiving two different DONE pins at close to the same
    # moment must not interleave their read/append/delete/save sequence and
    # silently drop one of them, even though both calls report success.
    from concurrent.futures import ThreadPoolExecutor

    pins = [_create(client, f"pin {i}") for i in range(6)]
    for pin in pins:
        client.patch(f"/api/v1/feedback/comments/{pin['id']}", json={"status": "fixed"})

    def _archive(pin: dict) -> int:
        return client.post(f"/api/v1/feedback/comments/{pin['id']}/archive").status_code

    with ThreadPoolExecutor(max_workers=len(pins)) as pool:
        codes = list(pool.map(_archive, pins))

    assert codes == [200] * len(pins)
    assert _comments(tmp_path) == [], "every archived pin must leave the live board"
    archived_texts = {
        c["text"] for f in _archives(tmp_path) for c in json.loads(f.read_text())["comments"]
    }
    assert archived_texts == {f"pin {i}" for i in range(len(pins))}, (
        "a lost interleave would drop one pin's write even though its request returned 200"
    )


# ----- follow-on ----------------------------------------------------------
def test_follow_on_makes_a_new_open_pin_at_the_same_anchor(
    client: TestClient, tmp_path: Path
) -> None:
    parent = _create(client, "jump to master button misaligned")
    client.patch(
        f"/api/v1/feedback/comments/{parent['id']}",
        json={
            "status": "merged",
            "issue_url": "https://github.com/maintainer/music-dj-tools/issues/888",
        },
    )

    r = client.post(f"/api/v1/feedback/comments/{parent['id']}/follow-on")
    assert r.status_code == 201, r.text
    child = r.json()

    assert child["id"] != parent["id"]
    assert (child["x_pct"], child["y_pct"], child["anchor"], child["page"]) == (
        parent["x_pct"],
        parent["y_pct"],
        parent["anchor"],
        parent["page"],
    )
    assert child["text"].startswith(
        "Follow-on to https://github.com/maintainer/music-dj-tools/issues/888:"
    ), "a follow-on that does not name its parent is an orphan - broken"
    assert child["status"] == "open"

    ids = {c["id"]: c for c in _comments(tmp_path)}
    assert ids[parent["id"]]["status"] == "merged", "the parent stays green"
    assert len(ids) == 2


def test_follow_on_falls_back_to_the_pin_id_and_carries_any_extra_text(
    client: TestClient,
) -> None:
    parent = _create(client)
    client.patch(f"/api/v1/feedback/comments/{parent['id']}", json={"status": "fixed"})
    child = client.post(
        f"/api/v1/feedback/comments/{parent['id']}/follow-on",
        json={"text": "still drops the 4th cue"},
    ).json()
    assert child["text"] == f"Follow-on to {parent['id']}: still drops the 4th cue"


@pytest.mark.parametrize("status", ["open", "issued", "blocked"])
def test_follow_on_of_an_unfinished_pin_is_rejected(
    client: TestClient, tmp_path: Path, status: str
) -> None:
    # The widget only offers Follow-on once a pin is fixed/merged, but this
    # endpoint is agent-facing (AGENTS.md) too, so the same invariant has to
    # be enforced here or an API caller can fork work still in flight.
    parent = _create(client)
    if status != "open":
        client.patch(f"/api/v1/feedback/comments/{parent['id']}", json={"status": status})
    before = (tmp_path / "feedback" / "comments.json").read_text()

    r = client.post(f"/api/v1/feedback/comments/{parent['id']}/follow-on")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "PIN_NOT_DONE"
    assert (tmp_path / "feedback" / "comments.json").read_text() == before, (
        "a rejected follow-on must leave the parent exactly where it was, with no child"
    )


def test_follow_on_of_an_unknown_pin_is_404(client: TestClient) -> None:
    r = client.post("/api/v1/feedback/comments/nope/follow-on")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "COMMENT_NOT_FOUND"


# ----- the bulk archive still works, and keeps live pin status ------------
def test_bulk_archive_marks_each_pin_harvested_and_keeps_it_on_the_board(
    client: TestClient, tmp_path: Path
) -> None:
    # FB-06 (amended) / FB-18b, issue #3981: the harvest snapshot carries the pin,
    # and the live pin stays in comments.json with operator status unchanged;
    # only the explicit per-pin archive removes one from the canvas.
    pin = _create(client)
    client.patch(
        f"/api/v1/feedback/comments/{pin['id']}",
        json={"status": "issued", "issue_url": "https://example.test/issues/1"},
    )
    out = client.post("/api/v1/feedback/archive").json()
    assert out["comments_archived"] == 1
    archived = json.loads(Path(out["archived_to"]).read_text())["comments"]
    assert archived[0]["id"] == pin["id"]
    assert archived[0]["agent_snapshot_at"]
    assert archived[0]["issue_url"].endswith("/1"), "the issue link survives the harvest"
    live = _comments(tmp_path)
    assert [c["id"] for c in live] == [pin["id"]], "no pin disappears from the board"
    assert live[0]["status"] == "issued"
    assert live[0]["agent_snapshot_at"] == archived[0]["agent_snapshot_at"]
    # Idempotent: a second harvest finds nothing new and writes no archive.
    again = client.post("/api/v1/feedback/archive").json()
    assert again["comments_archived"] == 0 and again["archived_to"] is None
    assert [c["status"] for c in _comments(tmp_path)] == ["issued"]


pytestmark = pytest.mark.rb_parity
