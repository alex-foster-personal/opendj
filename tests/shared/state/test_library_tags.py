"""Tags and tag types (Library Organization PRD R-TAG, R-TT, R-UNDO-1 for tags).

[if] a tag removal cannot be undone or two spellings make two tags [then] broken, [else stop].
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import library_org as org
from apps.shared.state import library_tags as tags
from apps.shared.state import schema as state_schema
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.deps import get_read_state, get_write_state
from apps.webui.server.routes import library_org as routes

pytestmark = pytest.mark.requirement("LIBTAG-7898")

S = "session-a"
HEADERS = {"X-Undo-Session": S}
_NOW = "2026-10-30T12:00:00.000000+00:00"


def _track(writer: StateWriter, stable_id: str, values: list[str] | None = None) -> None:
    writer.upsert_track(
        stable_id=stable_id,
        stable_id_tier="inferred",
        title=stable_id,
        artists=["A"],
        album=None,
        isrc=None,
        duration_ms=1000,
        file_path=f"/{stable_id}.mp3",
        content_hash=stable_id,
        audio_hash=None,
    )
    if values is not None:
        writer.set_field(stable_id, "tags", values, source="webui", modified_at=_NOW, confidence=1.0)


@pytest.fixture
def writer(tmp_path: Path) -> Iterator[StateWriter]:
    conn = state_db.open_rw(tmp_path / "state.db")
    state_schema.apply_migrations(conn)
    (tmp_path / "machine-id").write_text("a" * 32)
    w = StateWriter(conn, FakeEventBus(), actor="test")
    _track(w, "lacrimosa", ["Mozart", "choir"])
    _track(w, "toxic", ["pop"])
    _track(w, "palladio")
    yield w
    conn.close()


@pytest.fixture
def client(tmp_path: Path, writer: StateWriter) -> TestClient:
    app = FastAPI()
    app.state.state_db_path = tmp_path / "state.db"
    app.include_router(routes.router)
    app.dependency_overrides[get_read_state] = lambda: None
    app.dependency_overrides[get_write_state] = lambda: None
    return TestClient(app)


def _tags(writer: StateWriter) -> dict[str, list[str]]:
    return tags.track_tag_map(writer._conn)


def _names(rows: list[dict]) -> list[str]:
    return [row["name"] for row in rows]


def test_a_tag_with_no_meta_row_is_general(writer: StateWriter) -> None:
    """[if] a tag has no tag_meta row and is not treated as general [then] broken."""
    assert {t.name: t.type for t in tags.tag_catalog(writer._conn)}["pop"] == "general"


def test_case_and_width_variants_are_one_tag(writer: StateWriter) -> None:
    """[if] Piano, piano and fullwidth piano count as separate tags [then] broken."""
    _track(writer, "a", ["Piano"])
    _track(writer, "b", ["piano"])
    _track(writer, "c", ["ｐｉａｎｏ"])
    pianos = [t for t in tags.tag_catalog(writer._conn) if t.name_norm == "piano"]
    assert [t.track_count for t in pianos] == [3]


def test_assign_lands_on_the_existing_spelling_and_never_duplicates(writer: StateWriter) -> None:
    """[if] adding POP or re-adding a held tag creates a second tag [then] broken."""
    result = tags.assign_tags(writer, S, ["toxic", "palladio"], add=["POP"])
    assert result.row["tracks"] == {"toxic": ["pop"], "palladio": ["pop"]}
    assert tags.assign_tags(writer, S, ["toxic", "palladio"], add=["pop"]).undo_id is None


def test_every_selected_track_is_tagged(writer: StateWriter) -> None:
    """[if] tagging 3 selected tracks tags only one [then] broken."""
    tags.assign_tags(writer, S, ["lacrimosa", "toxic", "palladio"], add=["euphoric"])
    assert all("euphoric" in held for held in _tags(writer).values())
    assert len(_tags(writer)) == 3


def test_undo_restores_a_removed_tag(writer: StateWriter) -> None:
    """[if] undo after removing #Mozart does not restore it [then] broken (issue hard veto)."""
    tags.assign_tags(writer, S, ["lacrimosa"], remove=["mozart"])
    assert _tags(writer)["lacrimosa"] == ["choir"]
    assert org.undo_last(writer, S).op == "tag.assign"
    assert _tags(writer)["lacrimosa"] == ["Mozart", "choir"]


def test_undo_clears_a_first_tag(writer: StateWriter) -> None:
    tags.assign_tags(writer, S, ["palladio"], add=["euphoric"])
    org.undo_last(writer, S)
    assert "palladio" not in _tags(writer)


def test_undo_refuses_when_the_tags_changed_since(writer: StateWriter) -> None:
    """[if] undo overwrites a later change to the same track's tags [then] broken."""
    tags.assign_tags(writer, S, ["toxic"], add=["hot"])
    writer.set_field("toxic", "tags", ["pop", "hot", "synced"], source="webui", modified_at=_NOW, confidence=1.0)
    with pytest.raises(org.OrgError) as refused:
        org.undo_last(writer, S)
    assert refused.value.code == "CONFLICT"
    assert "synced" in _tags(writer)["toxic"]


def test_an_unknown_track_refuses_the_whole_assign(writer: StateWriter) -> None:
    with pytest.raises(org.OrgError) as refused:
        tags.assign_tags(writer, S, ["toxic", "nope"], add=["hot"])
    assert refused.value.code == "NOT_FOUND"
    assert _tags(writer)["toxic"] == ["pop"]


def test_rename_into_an_existing_tag_needs_merge_and_undoes(writer: StateWriter) -> None:
    """[if] renaming #pop to #choir silently loses assignments, or a merge cannot be undone [then] broken."""
    with pytest.raises(org.OrgError) as refused:
        tags.rename_tag(writer, S, "pop", "choir")
    assert refused.value.code == "CONFLICT"
    assert "1 track" in refused.value.message
    tags.assign_tags(writer, S, ["lacrimosa"], add=["pop"])
    merged = tags.rename_tag(writer, S, "pop", "CHOIR", merge=True)
    assert merged.row == {"name": "choir", "tracks_updated": 2, "merged": True}
    assert _tags(writer)["lacrimosa"] == ["Mozart", "choir"]
    assert _tags(writer)["toxic"] == ["choir"]
    org.undo_last(writer, S)
    assert _tags(writer)["toxic"] == ["pop"]
    assert _tags(writer)["lacrimosa"] == ["Mozart", "choir", "pop"]


def test_rename_carries_the_type_and_changes_case_in_place(writer: StateWriter) -> None:
    tags.set_tag_type(writer, S, "choir", "instrument")
    tags.rename_tag(writer, S, "choir", "chorus")
    assert {t.name: t.type for t in tags.tag_catalog(writer._conn)}["chorus"] == "instrument"
    tags.rename_tag(writer, S, "chorus", "Chorus")
    assert _tags(writer)["lacrimosa"] == ["Mozart", "Chorus"]
    assert [t.name for t in tags.tag_catalog(writer._conn) if t.name_norm == "chorus"] == ["Chorus"]


def test_delete_tag_undoes_with_its_type(writer: StateWriter) -> None:
    tags.set_tag_type(writer, S, "choir", "instrument")
    assert tags.delete_tag(writer, S, "choir").row == {"tracks_updated": 1}
    assert "choir" not in {t.name for t in tags.tag_catalog(writer._conn)}
    org.undo_last(writer, S)
    assert {t.name: t.type for t in tags.tag_catalog(writer._conn)}["choir"] == "instrument"
    assert _tags(writer)["lacrimosa"] == ["Mozart", "choir"]


def test_create_is_idempotent_and_one_tag_per_normal_form(writer: StateWriter) -> None:
    """[if] creating Piano when piano exists makes a second tag [then] broken."""
    first = tags.create_tag(writer, S, "Harp", "instrument")
    assert first.undo_id
    assert tags.create_tag(writer, S, "harp", "instrument").undo_id is None
    assert [t.name for t in tags.tag_catalog(writer._conn) if t.name_norm == "harp"] == ["Harp"]
    with pytest.raises(org.OrgError) as refused:
        tags.create_tag(writer, S, "harp", "mood")
    assert refused.value.code == "CONFLICT"


def test_a_new_name_from_a_type_column_lands_in_that_type(writer: StateWriter) -> None:
    """[if] creating harp from the Instruments column makes a general tag [then] broken."""
    tags.assign_tags(writer, S, ["toxic"], add=["harp"], tag_type="instrument")
    assert {t.name: t.type for t in tags.tag_catalog(writer._conn)}["harp"] == "instrument"


def test_deleting_a_type_retypes_its_tags_to_general(writer: StateWriter) -> None:
    """[if] deleting a type deletes its tags [then] broken."""
    tags.set_tag_type(writer, S, "choir", "mood")
    org.delete_tag_type(writer, S, "mood")
    assert {t.name: t.type for t in tags.tag_catalog(writer._conn)}["choir"] == "general"
    assert _tags(writer)["lacrimosa"] == ["Mozart", "choir"]
    org.undo_last(writer, S)
    assert {t.name: t.type for t in tags.tag_catalog(writer._conn)}["choir"] == "mood"
    with pytest.raises(org.OrgError):
        org.delete_tag_type(writer, S, "general")


def test_suggest_literal_plural_and_similar(writer: StateWriter) -> None:
    """[if] violins misses violin, or sad does not offer dark marked similar [then] broken."""
    _track(writer, "x", ["violin", "dark"])
    plural = tags.suggest_tags(writer._conn, "violins")
    assert (_names(plural), plural[0]["similar"]) == (["violin"], False)
    sad = tags.suggest_tags(writer._conn, "sad")
    assert (_names(sad), sad[0]["similar"]) == (["dark"], True)


def test_suggest_leaves_out_a_tag_every_selected_track_holds(writer: StateWriter) -> None:
    """[if] an already-applied tag is suggested again for the same track [then] broken."""
    assert "pop" not in _names(tags.suggest_tags(writer._conn, "pop", track_ids=["toxic"]))
    assert "pop" in _names(tags.suggest_tags(writer._conn, "pop", track_ids=["toxic", "palladio"]))


def test_suggest_stays_inside_a_type(writer: StateWriter) -> None:
    """[if] the Instruments editor suggests a general tag [then] broken."""
    tags.set_tag_type(writer, S, "choir", "instrument")
    assert _names(tags.suggest_tags(writer._conn, "", tag_type="instrument")) == ["choir"]
    assert tags.suggest_tags(writer._conn, "pop", tag_type="instrument") == []


def test_learned_synonyms_are_used(writer: StateWriter) -> None:
    org.put_tag_synonym(writer, S, "banging", "pop")
    assert _names(tags.suggest_tags(writer._conn, "banging")) == ["pop"]


def test_http_assign_undo_and_read(client: TestClient) -> None:
    missing = client.post("/mytags/assignments", json={"stable_ids": ["toxic"], "add": ["x"]})
    assert missing.status_code == 422
    done = client.post("/mytags/assignments", json={"stable_ids": ["toxic"], "remove": ["pop"]}, headers=HEADERS)
    assert done.status_code == 200
    assert done.json()["undo_id"]
    assert "toxic" not in client.get("/mytags/assignments").json()["tracks"]
    assert client.post("/undo", headers=HEADERS).json()["op"] == "tag.assign"
    assert client.get("/mytags/assignments").json()["tracks"]["toxic"] == ["pop"]


def test_http_rename_conflict_merge_type_and_delete(client: TestClient) -> None:
    conflict = client.patch("/mytags/pop", json={"name": "choir"}, headers=HEADERS)
    assert (conflict.status_code, conflict.json()["detail"]["error"]) == (409, "CONFLICT")
    merged = client.patch("/mytags/pop", json={"name": "choir", "merge": True}, headers=HEADERS)
    assert merged.json()["row"]["merged"] is True
    retyped = client.patch("/mytags/choir", json={"type": "instrument"}, headers=HEADERS)
    assert retyped.json()["row"]["type"] == "instrument"
    assert client.patch("/mytags/choir", json={}, headers=HEADERS).status_code == 422
    assert client.delete("/mytags/choir", headers=HEADERS).status_code == 200
    assert client.delete("/mytags/choir", headers=HEADERS).status_code == 404


def test_http_a_name_with_a_slash_reaches_the_route(client: TestClient) -> None:
    client.post("/mytags/assignments", json={"stable_ids": ["toxic"], "add": ["drum/bass"]}, headers=HEADERS)
    renamed = client.patch("/mytags/drum/bass", json={"name": "dnb"}, headers=HEADERS)
    assert renamed.json()["row"]["tracks_updated"] == 1


def test_http_create_suggest_catalog_and_delete_type(client: TestClient) -> None:
    assert client.post("/mytags", json={"name": "Harp", "type": "instrument"}, headers=HEADERS).status_code == 201
    names = client.get("/mytags/suggest", params={"q": "ha", "type": "instrument"}).json()
    assert [row["name"] for row in names["suggestions"]] == ["Harp"]
    catalog = {row["name"]: row["type"] for row in client.get("/mytags/catalog").json()["tags"]}
    assert (catalog["Harp"], catalog["pop"]) == ("instrument", "general")
    assert client.delete("/tag-types/instrument", headers=HEADERS).json()["undo_id"]
    catalog = {row["name"]: row["type"] for row in client.get("/mytags/catalog").json()["tags"]}
    assert catalog["Harp"] == "general"
