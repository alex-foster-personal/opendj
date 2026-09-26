"""HTTP / CLI / UI parity for user-ordered stems and lyrics jobs (#1865).

[if] the UI can enqueue, list, reorder, or cancel a library job [then] HTTP and
CLI expose the same verbs, else stop.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.analysis import queue_cli
from apps.webui.server.routes import library_jobs

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
OPENAPI: Path = REPO_ROOT / "apps" / "webui" / "openapi.json"
PANEL: Path = (
    REPO_ROOT
    / "apps/webui/frontend/src/lib/components/rb/library-jobs/LibraryJobQueuePanel.svelte"
)
BROWSER: Path = (
    REPO_ROOT / "apps/webui/frontend/src/lib/components/rb/BrowserPanel.svelte"
)
API_CLIENT: Path = (
    REPO_ROOT / "apps/webui/frontend/src/lib/rb/api-library-jobs.ts"
)

CONTROLS: tuple[tuple[str, str], ...] = (
    ("enqueue", "user-enqueue"),
    ("", "user-list"),
    ("{lane}/{stable_id}", "user-reorder"),
    ("{lane}/{stable_id}/cancel", "user-cancel"),
)


def _router_paths() -> set[str]:
    return {route.path for route in library_jobs.router.routes}


def _cli_subcommands() -> set[str]:
    parser = queue_cli.build_parser()
    subparser_actions = [
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    assert subparser_actions
    return set(subparser_actions[0]._name_parser_map)


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    from apps.webui.server.app import create_app

    app = create_app()
    app.state.analysis_db_path = tmp_path / "state.db"
    app.state.stem_roots = (tmp_path / "stems",)
    return TestClient(app)


def _seed_library(db: Path, ids: list[str]) -> None:
    from apps.analysis.store import open_conn

    conn = open_conn(db)
    for stable_id in ids:
        audio = db.parent / f"{stable_id}.wav"
        audio.write_bytes(b"\0")
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, duration_ms, "
            "file_path, created_at, updated_at) VALUES (?, 'inferred', ?, 180000, "
            "?, '2026-09-11T00:00:00Z', '2026-09-11T00:00:00Z')",
            (stable_id, stable_id, str(audio)),
        )
    conn.commit()
    conn.close()


# REQ: PERFBATCH-05
@pytest.mark.requirement("PERFBATCH-05")
def test_every_control_exists_on_http_and_on_the_cli() -> None:
    """[if] a library-jobs control exists [then] it has an HTTP route and CLI verb, [else stop]."""
    paths = _router_paths()
    subcommands = _cli_subcommands()
    assert "/library-jobs/enqueue" in paths
    assert "/library-jobs" in paths or "" in {p.rstrip("/") for p in paths}
    assert "user-enqueue" in subcommands
    assert "user-list" in subcommands
    assert "user-reorder" in subcommands
    assert "user-cancel" in subcommands


# REQ: PERFBATCH-05
@pytest.mark.requirement("PERFBATCH-05")
def test_every_control_is_documented_in_openapi_json() -> None:
    """[if] a library-jobs route exists [then] openapi.json documents it, [else stop]."""
    spec = json.loads(OPENAPI.read_text())
    documented = set(spec["paths"])
    assert "/api/v1/library-jobs/enqueue" in documented
    assert "/api/v1/library-jobs" in documented
    assert "/api/v1/library-jobs/{lane}/{stable_id}" in documented
    assert "/api/v1/library-jobs/{lane}/{stable_id}/cancel" in documented


# REQ: PERFBATCH-05
@pytest.mark.requirement("PERFBATCH-05")
def test_every_control_is_reachable_from_the_ui_panel() -> None:
    """[if] a library-jobs control exists [then] the UI panel reaches it, [else stop]."""
    assert PANEL.exists(), f"{PANEL} is missing; the UI half of parity is absent"
    assert API_CLIENT.exists(), f"{API_CLIENT} is missing"
    client_src = API_CLIENT.read_text()
    panel_src = PANEL.read_text()
    browser_src = BROWSER.read_text()
    for needle in (
        "library-jobs/enqueue",
        "enqueueLibraryJobs",
        "listLibraryJobs",
        "reorderLibraryJob",
        "cancelLibraryJob",
    ):
        assert needle in client_src, needle
    # Enqueue is the library menu; list/reorder/cancel live in the panel.
    assert "enqueueLibraryJobsBatched" in browser_src
    for fn in ("listLibraryJobs", "reorderLibraryJob", "cancelLibraryJob"):
        assert fn in panel_src, fn


def test_enqueue_empty_list_is_422(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/library-jobs/enqueue",
        json={"lane": "stems", "stable_ids": []},
    )
    assert resp.status_code == 422


def test_enqueue_unknown_id_names_it(client: TestClient, tmp_path: Path) -> None:
    _seed_library(tmp_path / "state.db", ["known"])
    resp = client.post(
        "/api/v1/library-jobs/enqueue",
        json={"lane": "stems", "stable_ids": ["known", "ghost"]},
    )
    assert resp.status_code == 422
    assert "ghost" in resp.text


def test_get_order_matches_selection_order(client: TestClient, tmp_path: Path) -> None:
    _seed_library(tmp_path / "state.db", ["a", "b", "c"])
    resp = client.post(
        "/api/v1/library-jobs/enqueue",
        json={"lane": "stems", "stable_ids": ["c", "a", "b"], "placement": "next"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert [i["stable_id"] for i in body["items"]] == ["c", "a", "b"]
    listed = client.get("/api/v1/library-jobs", params={"lane": "stems"}).json()
    assert [i["stable_id"] for i in listed["items"]] == ["c", "a", "b"]
    assert listed["counts"]["pending"] == 3


def test_lyrics_and_stems_are_separate_lanes(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_library(tmp_path / "state.db", ["a", "b"])
    stems = client.post(
        "/api/v1/library-jobs/enqueue",
        json={"lane": "stems", "stable_ids": ["a"]},
    )
    lyrics = client.post(
        "/api/v1/library-jobs/enqueue",
        json={"lane": "lyrics", "stable_ids": ["b"]},
    )
    assert stems.status_code == 201 and lyrics.status_code == 201
    s_list = client.get("/api/v1/library-jobs", params={"lane": "stems"}).json()
    l_list = client.get("/api/v1/library-jobs", params={"lane": "lyrics"}).json()
    assert [i["stable_id"] for i in s_list["items"]] == ["a"]
    assert [i["stable_id"] for i in l_list["items"]] == ["b"]


def test_patch_then_cancel(client: TestClient, tmp_path: Path) -> None:
    _seed_library(tmp_path / "state.db", ["a", "b", "c"])
    client.post(
        "/api/v1/library-jobs/enqueue",
        json={"lane": "stems", "stable_ids": ["a", "b", "c"]},
    )
    patched = client.patch(
        "/api/v1/library-jobs/stems/c",
        json={"before_stable_id": "a"},
    )
    assert patched.status_code == 200, patched.text
    listed = client.get("/api/v1/library-jobs", params={"lane": "stems"}).json()
    assert [i["stable_id"] for i in listed["items"]] == ["c", "a", "b"]
    cancelled = client.post("/api/v1/library-jobs/stems/a/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    left = client.get("/api/v1/library-jobs", params={"lane": "stems"}).json()
    assert [i["stable_id"] for i in left["items"]] == ["c", "b"]
