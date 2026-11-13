"""Add My Tag to Comments setting: HTTP parity, sync on every tag or comment write, sweep, export (issue #10025).

[if] the setting is on [then] each track's comment ends in one '/* a / b */' block kept in sync with its tags, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.sync.usb.pioneer.pdb_writer import PdbExport, PdbTrack, write_export_pdb
from apps.sync.usb.pioneer.reader import read_export_pdb
from apps.webui.server.backend import InMemoryBackend
from tests.webui.app_fixtures import client_fixture  # noqa: F401  (pytest fixtures)

from .conftest import current_etag

pytestmark = pytest.mark.requirement("META-14581")

USER_TEXT = "  Big room drop at 2:10 "


@pytest.fixture(name="mt_client")
def mt_client_fixture(client: TestClient, tmp_path: Path) -> TestClient:
    client.app.state.data_dir = tmp_path / "data"
    return client


def _set(client: TestClient, enabled: bool) -> dict:
    response = client.put("/api/v1/mytag-comments", json={"enabled": enabled})
    assert response.status_code == 200, response.text
    return response.json()


def _patch(client: TestClient, backend: InMemoryBackend, stable_id: str, body: dict) -> dict:
    response = client.patch(
        f"/api/v1/tracks/{stable_id}", json=body, headers={"If-Match": current_etag(backend, stable_id)}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _comments(backend: InMemoryBackend, stable_id: str) -> str | None:
    return backend.get_track(stable_id).comments


def test_assign_and_patch_tags_rewrite_the_comment_keeping_user_text(mt_client, seed_backend) -> None:
    """[if] on and tags change via mytags/assign or PATCH [then] comment is '<user> /* tags */', user bytes kept, [else stop]."""
    _patch(mt_client, seed_backend, "track-001", {"comments": USER_TEXT})
    _set(mt_client, True)
    assign = mt_client.post(
        "/api/v1/mytags/assign",
        json={
            "stable_ids": ["track-001"],
            "expected_etags": {"track-001": current_etag(seed_backend, "track-001")},
            "add": ["openers"],
        },
    )
    assert assign.status_code == 200, assign.text
    assert _comments(seed_backend, "track-001") == f"{USER_TEXT} /* deep-house / openers */"
    assert assign.json()["results"][0]["etag"] == current_etag(seed_backend, "track-001"), "if a stale etag then broken"
    patched = _patch(mt_client, seed_backend, "track-001", {"tags_remove": ["deep-house"]})
    assert patched["comments"] == f"{USER_TEXT} /* openers */", "if PATCH tags do not re-render the block then broken"


def test_turning_off_strips_every_track_and_on_rebuilds_idempotently(mt_client, seed_backend) -> None:
    """[if] PUT on then off, each twice [then] 5 rewritten, then 0, 5 stripped, then 0, [else stop]."""
    _patch(mt_client, seed_backend, "track-003", {"comments": USER_TEXT})
    assert _set(mt_client, True) == {"enabled": True, "tracks_updated": 5}
    assert _set(mt_client, True)["tracks_updated"] == 0, "if a second on sweep rewrites again then broken"
    assert _comments(seed_backend, "track-003") == f"{USER_TEXT} /* ambient / downtempo */"
    assert _comments(seed_backend, "track-002") == "/* techno */"
    assert _set(mt_client, False) == {"enabled": False, "tracks_updated": 5}
    assert _set(mt_client, False)["tracks_updated"] == 0, "if a second off sweep rewrites again then broken"
    assert _comments(seed_backend, "track-003") == USER_TEXT, "if off does not give back the user text then broken"
    assert _comments(seed_backend, "track-002") == "", "if off leaves the block then broken"


def test_comment_edit_changes_only_the_user_part(mt_client, seed_backend) -> None:
    """[if] on and a comment is edited, with or without the block text [then] one block trails the new text, [else stop]."""
    _set(mt_client, True)
    assert _patch(mt_client, seed_backend, "track-002", {"comments": "new note"})["comments"] == "new note /* techno */"
    shown = _comments(seed_backend, "track-002")
    edited = _patch(mt_client, seed_backend, "track-002", {"comments": shown.replace("new", "old")})
    assert edited["comments"] == "old note /* techno */", "if the block is duplicated or lost then broken"


def test_rename_delete_and_bulk_edit_keep_the_block_in_sync(mt_client, seed_backend) -> None:
    """[if] on and a tag is renamed, deleted or bulk-added [then] each affected comment block follows, [else stop]."""
    _set(mt_client, True)
    catalog = mt_client.get("/api/v1/mytags").json()
    rename = mt_client.post(
        "/api/v1/mytags/rename",
        json={
            "old_name": "techno",
            "new_name": "peak",
            "expected_catalog_revision": catalog["catalog_revision"],
            "expected_track_count": 1,
        },
    )
    assert rename.status_code == 200, rename.text
    assert _comments(seed_backend, "track-002") == "/* peak */", "if rename leaves the old tag in the block then broken"
    catalog = mt_client.get("/api/v1/mytags").json()
    delete = mt_client.post(
        "/api/v1/mytags/delete",
        json={"name": "peak", "expected_catalog_revision": catalog["catalog_revision"], "expected_track_count": 1},
    )
    assert delete.status_code == 200, delete.text
    assert _comments(seed_backend, "track-002") == "", "if delete leaves the block then broken"
    bulk = mt_client.patch(
        "/api/v1/bulk-edit",
        json={
            "stable_ids": ["track-004"],
            "expected_etags": {"track-004": current_etag(seed_backend, "track-004")},
            "tags_add": ["closers"],
        },
    )
    assert bulk.status_code == 200, bulk.text
    assert _comments(seed_backend, "track-004") == "/* trance / closers */", "if bulk edit skips the block then broken"


def test_usb_export_writes_the_merged_comment_or_the_user_comment(mt_client, seed_backend, tmp_path: Path) -> None:
    """[if] export.pdb is written from stored comments [then] on carries the block, off the user text only, [else stop]."""
    _patch(mt_client, seed_backend, "track-001", {"comments": USER_TEXT})

    def exported_comment(name: str) -> str:
        track = seed_backend.get_track("track-001")
        root = tmp_path / name
        pdb = PdbTrack(id=1, title=track.title or "", file_path="/Contents/a.flac", comment=track.comments or "")
        write_export_pdb(root, PdbExport(tracks=(pdb,)))
        return read_export_pdb(root)["tracks"][0]["comment"]

    _set(mt_client, True)
    assert exported_comment("on") == f"{USER_TEXT} /* deep-house */", "if the export loses the block then broken"
    _set(mt_client, False)
    assert exported_comment("off") == USER_TEXT, "if the export keeps the block when off then broken"


def test_agent_parity_reads_and_writes_the_setting_over_http(mt_client) -> None:
    """[if] GET, PUT on, GET [then] off by default, then on, persisted in mytag-comments.json, [else stop]."""
    assert mt_client.get("/api/v1/mytag-comments").json() == {"enabled": False}, "if default is not off then broken"
    _set(mt_client, True)
    assert mt_client.get("/api/v1/mytag-comments").json() == {"enabled": True}, "if the write is not read back"
    stored = Path(mt_client.app.state.data_dir) / "state" / "mytag-comments.json"
    assert stored.read_text(encoding="utf-8") == '{"enabled": true}', "if the setting is not persisted then broken"
    assert mt_client.put("/api/v1/mytag-comments", json={"enabled": "yes"}).status_code == 422, "if non-bool accepted"
