"""In-app feedback store + endpoints (FB-02..FB-05).

Regression lines:
- if a created todo does not land in data-dir/feedback/review-todos.json then
  the store round-trip is broken
- if PATCHing done/feedback/chosen_option does not persist to disk then the maintainer's
  review is lost on reload
- if a chosen_option outside the todo's options is accepted then the choice
  modal can record an impossible answer
- if a pin outside 0..100 percent is accepted then it renders off-viewport
- if the general note loses its build stamp then feedback no longer maps to
  the version it was given on
- if archive deletes anything instead of moving it then feedback is lost
- if a second archive re-archives already-harvested feedback then zTasks.md
  grows duplicates
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend


@pytest.fixture
def fb(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
    )
    app.state.data_dir = data_dir
    with TestClient(app) as client:
        client.data_dir = data_dir  # type: ignore[attr-defined]
        yield client


def _feedback_dir(client: TestClient) -> Path:
    return client.data_dir / "feedback"  # type: ignore[attr-defined]


# ----- todos --------------------------------------------------------------
@pytest.mark.requirement("FB-02")
def test_todo_create_list_roundtrip_hits_disk(fb: TestClient) -> None:
    r = fb.post(
        "/api/v1/feedback/todos",
        json={"title": "Review the panel", "detail": "drag it around"},
    )
    assert r.status_code == 201
    created = r.json()
    assert created["done"] is False
    assert created["feedback"] == ""
    assert created["build"]["git_sha"], "repo checkout must stamp a sha"

    listed = fb.get("/api/v1/feedback/todos").json()["todos"]
    assert [t["id"] for t in listed] == [created["id"]]

    disk = json.loads(
        (_feedback_dir(fb) / "review-todos.json").read_text(encoding="utf-8")
    )
    assert disk["todos"][0]["title"] == "Review the panel"


@pytest.mark.requirement("FB-02")
def test_todo_patch_done_feedback_and_option_persist(fb: TestClient) -> None:
    created = fb.post(
        "/api/v1/feedback/todos",
        json={"title": "Pick a layout", "options": ["compact", "roomy"]},
    ).json()
    r = fb.patch(
        f"/api/v1/feedback/todos/{created['id']}",
        json={"done": True, "feedback": "compact wins", "chosen_option": "compact"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["done"] is True
    assert body["chosen_option"] == "compact"

    disk = json.loads(
        (_feedback_dir(fb) / "review-todos.json").read_text(encoding="utf-8")
    )
    assert disk["todos"][0]["done"] is True
    assert disk["todos"][0]["feedback"] == "compact wins"


@pytest.mark.requirement("FB-02")
def test_todo_patch_unknown_id_404s(fb: TestClient) -> None:
    r = fb.patch("/api/v1/feedback/todos/nope", json={"done": True})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "FEEDBACK_TODO_NOT_FOUND"


@pytest.mark.requirement("FB-02")
def test_todo_chosen_option_outside_options_422s(fb: TestClient) -> None:
    created = fb.post(
        "/api/v1/feedback/todos",
        json={"title": "Pick one", "options": ["a", "b"]},
    ).json()
    r = fb.patch(
        f"/api/v1/feedback/todos/{created['id']}", json={"chosen_option": "c"}
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "FEEDBACK_INVALID"


@pytest.mark.requirement("FB-02")
def test_todo_empty_title_422s(fb: TestClient) -> None:
    assert fb.post("/api/v1/feedback/todos", json={"title": ""}).status_code == 422


@pytest.mark.requirement("FB-02")
def test_todo_patch_rejects_explicit_null_title_and_does_not_advertise_it(
    fb: TestClient,
) -> None:
    todo = fb.post("/api/v1/feedback/todos", json={"title": "original"}).json()

    response = fb.patch(f"/api/v1/feedback/todos/{todo['id']}", json={"title": None})

    assert response.status_code == 422, response.text
    title_schema = fb.get("/openapi.json").json()["components"]["schemas"][
        "TodoPatchIn"
    ]["properties"]["title"]
    assert "anyOf" not in title_schema, (
        "if title accepts null in OpenAPI then typed clients can send a request "
        "the server cannot store - broken"
    )


# ----- comments -----------------------------------------------------------
@pytest.mark.requirement("FB-03")
def test_comment_roundtrip_and_bounds(fb: TestClient) -> None:
    r = fb.post(
        "/api/v1/feedback/comments",
        json={
            "x_pct": 41.5,
            "y_pct": 12.0,
            "anchor": "#vibe-meter",
            "page": "/performance",
            "text": "this overlaps the clock",
            "ui": "chrome-loop",
            "viewport_width": 1280,
            "viewport_height": 800,
        },
    )
    assert r.status_code == 201
    pin = r.json()
    assert pin["build"]["git_sha"]
    assert pin["environment"] == {
        "ui": "chrome-loop",
        "viewport_width": 1280,
        "viewport_height": 800,
        "machine": "test-host",
        "release_version": "0.1.0",
        "user_email": None,
        "ui_config": None,
    }

    listed = fb.get("/api/v1/feedback/comments").json()["comments"]
    assert [c["id"] for c in listed] == [pin["id"]]

    off = fb.post(
        "/api/v1/feedback/comments",
        json={"x_pct": 140, "y_pct": 5, "page": "/performance", "text": "x"},
    )
    assert off.status_code == 422


# ----- general note -------------------------------------------------------
@pytest.mark.requirement("FB-04")
def test_general_note_roundtrip_with_build_stamp(fb: TestClient) -> None:
    empty = fb.get("/api/v1/feedback/general").json()
    assert empty == {"text": "", "updated_at": None, "build": None}

    r = fb.put("/api/v1/feedback/general", json={"text": "love the chevron"})
    assert r.status_code == 200
    note = r.json()
    assert note["text"] == "love the chevron"
    assert note["updated_at"] is not None
    assert note["build"]["git_sha"], "note must map to the build it was given on"

    disk = json.loads(
        (_feedback_dir(fb) / "general-note.json").read_text(encoding="utf-8")
    )
    assert disk["text"] == "love the chevron"


# ----- archive ------------------------------------------------------------
@pytest.mark.requirement("FB-06")
def test_archive_moves_never_deletes(fb: TestClient) -> None:
    done = fb.post("/api/v1/feedback/todos", json={"title": "done item"}).json()
    fb.patch(f"/api/v1/feedback/todos/{done['id']}", json={"done": True})
    open_todo = fb.post(
        "/api/v1/feedback/todos", json={"title": "still open"}
    ).json()
    fb.patch(
        f"/api/v1/feedback/todos/{open_todo['id']}",
        json={"feedback": "needs bigger text"},
    )
    fb.post(
        "/api/v1/feedback/comments",
        json={
            "x_pct": 1,
            "y_pct": 1,
            "page": "/performance",
            "text": "pin",
            "ui": "chrome-loop",
            "viewport_width": 1280,
            "viewport_height": 800,
        },
    )
    fb.put("/api/v1/feedback/general", json={"text": "general thoughts"})

    r = fb.post("/api/v1/feedback/archive")
    assert r.status_code == 200
    out = r.json()
    assert out["todos_archived"] == 1
    assert out["todo_feedback_archived"] == 1
    assert out["comments_archived"] == 1
    assert out["general_archived"] is True

    archive = json.loads(Path(out["archived_to"]).read_text(encoding="utf-8"))
    assert archive["todos"][0]["title"] == "done item"
    assert archive["todo_feedback"][0]["feedback"] == "needs bigger text"
    assert archive["comments"][0]["text"] == "pin"
    assert archive["general"]["text"] == "general thoughts"

    # The open todo survives with its harvested feedback reset; pins stay on the board.
    todos = fb.get("/api/v1/feedback/todos").json()["todos"]
    assert [t["title"] for t in todos] == ["still open"]
    assert todos[0]["feedback"] == ""
    comments = fb.get("/api/v1/feedback/comments").json()["comments"]
    assert len(comments) == 1
    assert comments[0]["text"] == "pin"
    assert comments[0]["agent_snapshot_at"]
    assert fb.get("/api/v1/feedback/general").json()["text"] == ""


@pytest.mark.requirement("FB-06")
def test_second_archive_with_nothing_new_writes_no_file(fb: TestClient) -> None:
    fb.put("/api/v1/feedback/general", json={"text": "once"})
    first = fb.post("/api/v1/feedback/archive").json()
    assert first["archived_to"] is not None

    second = fb.post("/api/v1/feedback/archive").json()
    assert second["archived_to"] is None
    assert second["general_archived"] is False

    archives = sorted(_feedback_dir(fb).glob("archive-*.json"))
    assert len(archives) == 1


# ----- contract -----------------------------------------------------------
@pytest.mark.requirement("FB-05")
def test_feedback_paths_present_in_openapi(fb: TestClient) -> None:
    spec = fb.get("/openapi.json").json()
    for path in (
        "/api/v1/feedback/todos",
        "/api/v1/feedback/todos/{todo_id}",
        "/api/v1/feedback/comments",
        "/api/v1/feedback/comments/{comment_id}/replies",
        "/api/v1/feedback/general",
        "/api/v1/feedback/archive",
    ):
        assert path in spec["paths"], f"{path} missing from the contract"

pytestmark = pytest.mark.rb_parity
