"""Semantic retrieval entry points (issue #11247, parent #2491).

Acceptance lines (SEARCH-NAV-13623):
- [if] a caller asks for audio-reference retrieval [then] ONE path is not free-text or hybrid, [else stop].
- [if] hybrid combines tags and text [then] factors name tag, embedding, and filter, [else stop].
- [if] results use embedding distance [then] ranking_kind is embedding_candidate, [else stop].
- [if] no vendor account exists [then] local baseline still returns candidates, [else stop].
- [if] only five tracks are indexed [then] similarity quality is not claimed, [else stop].
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.genre_infer import claim_envelope, findings, store
from apps.genre_infer.providers import enrichment_layers_available
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server import search_index, semantic_search
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("SEARCH-NAV-13623")

_VEC_A = [1.0, 0.0, 0.0]
_VEC_B = [0.9, 0.1, 0.0]
_VEC_C = [0.0, 1.0, 0.0]
_VEC_D = [0.85, 0.15, 0.0]
_VEC_E = [0.0, 0.0, 1.0]


def _claim(stable_id: str, dimension: str, value: str) -> claim_envelope.EnrichmentClaim:
    return claim_envelope.vendor_observation(
        stable_id=stable_id,
        dimension=dimension,
        value=value,
        source="local-test",
        provider_model="fixture",
        schema_version="fixture/1",
        input_fingerprint="fp",
        raw_evidence_ptr=f"fixture://{stable_id}",
    )


def _description(stable_id: str, text: str) -> claim_envelope.EnrichmentClaim:
    return claim_envelope.EnrichmentClaim(
        stable_id=stable_id,
        dimension="description",
        kind="generated_description",
        source="local-test",
        provider_model="fixture",
        schema_version="fixture/1",
        scope=claim_envelope.ClaimScope(kind="track"),
        value=text,
        confidence=0.9,
        generated_at="2026-11-12T00:00:00+00:00",
        input_fingerprint="fp",
        raw_evidence_ptr=f"fixture://{stable_id}",
    )


@pytest.fixture
def library(tmp_path: Path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    state_path = data_dir / "state" / "state.db"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    findings.ensure_schema(conn)
    sids = ["ref", "near", "far", "tagged", "other"]
    for sid in sids:
        writer.upsert_track(
            stable_id=sid,
            stable_id_tier="inferred",
            title=sid,
            artists=["Artist"],
            album=None,
            isrc=None,
            duration_ms=180_000,
            file_path=f"/music/{sid}.mp3",
        )
    conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, modified_at) "
        "VALUES ('tagged', 'genre', ?, 'manual', '2026-11-12T00:00:00Z')",
        (json.dumps("house"),),
    )
    findings.put_claim(conn, _claim("tagged", "mood", "euphoric"))
    findings.put_claim(conn, _claim("near", "mood", "driving"))
    findings.put_claim(conn, _description("near", "sunset beach vocal energy"))
    findings.put_claim(conn, _description("far", "ambient drone texture"))
    findings.put_claim(conn, _description("other", "warehouse techno pulse"))
    conn.commit()
    conn.close()
    store.append_embeddings(
        data_dir,
        [
            {"stable_id": "ref", "model": "clap/fixture", "revision": "r1", "status": "ok", "vector": _VEC_A},
            {"stable_id": "near", "model": "clap/fixture", "revision": "r1", "status": "ok", "vector": _VEC_B},
            {"stable_id": "far", "model": "clap/fixture", "revision": "r1", "status": "ok", "vector": _VEC_C},
            {"stable_id": "tagged", "model": "clap/fixture", "revision": "r1", "status": "ok", "vector": _VEC_D},
            {"stable_id": "other", "model": "clap/fixture", "revision": "r1", "status": "ok", "vector": _VEC_E},
        ],
    )
    return data_dir, state_path


@pytest.fixture
def client(library: tuple[Path, Path]) -> TestClient:
    data_dir, state_path = library
    app = create_app()
    app.state.backend = SqliteBackend(state_path)
    app.state.state_db_path = state_path
    app.state.data_dir = data_dir
    return TestClient(app)


def test_audio_reference_path_is_separate_from_free_text_and_hybrid(client: TestClient) -> None:
    """[if] caller asks for audio-reference retrieval [then] the API path is separate from free-text and hybrid, [else stop]."""
    audio = client.get("/api/v1/search/audio-reference", params={"reference_stable_id": "ref"})
    free = client.get("/api/v1/search/semantic/free-text", params={"q": "sunset vocal"})
    hybrid = client.get("/api/v1/search/semantic/hybrid", params={"q": "sunset", "tags": "mood:euphoric"})
    assert audio.status_code == 200
    assert free.status_code == 200
    assert hybrid.status_code == 200
    assert audio.json()["mode"] == "audio_reference"
    assert free.json()["mode"] == "free_text"
    assert hybrid.json()["mode"] == "hybrid"
    assert {audio.request.url.path, free.request.url.path, hybrid.request.url.path} == {
        "/api/v1/search/audio-reference",
        "/api/v1/search/semantic/free-text",
        "/api/v1/search/semantic/hybrid",
    }


def test_hybrid_response_names_tag_embedding_and_filter_factors(client: TestClient) -> None:
    """[if] hybrid combines tags and text [then] factors name tag, embedding, and filter, [else stop]."""
    response = client.get(
        "/api/v1/search/semantic/hybrid",
        params={"q": "sunset vocal", "tags": "mood:euphoric", "reference_stable_id": "ref"},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["mode"] == "hybrid"
    assert body["items"], "expected at least one hybrid candidate"
    kinds = {f["kind"] for item in body["items"] for f in item["factors"]}
    assert {"tag", "embedding", "filter"}.issubset(kinds)
    assert body["mix_compatible"] is False
    assert len(kinds) >= 3


def test_embedding_ranked_results_are_labelled_candidates_not_mix_compatible(client: TestClient) -> None:
    """[if] results are ranked by embedding distance [then] they are labelled candidates not mix-compatible, [else stop]."""
    body = client.get("/api/v1/search/audio-reference", params={"reference_stable_id": "ref"}).json()
    assert body["ranking_kind"] == "embedding_candidate"
    assert body["mix_compatible"] is False
    assert all(item["ranking_kind"] == "embedding_candidate" for item in body["items"])


def test_local_baseline_works_without_vendor_account(library: tuple[Path, Path]) -> None:
    """[if] no vendor account exists [then] local baseline still returns ranked candidates, [else stop]."""
    data_dir, state_path = library
    assert enrichment_layers_available() is True
    conn = state_db.open_ro(state_path)
    try:
        findings.ensure_schema(conn)
        result = semantic_search.audio_reference_retrieval(conn, data_dir, reference_stable_id="ref", limit=5)
    finally:
        conn.close()
    assert result.report.baseline.startswith("local")
    assert result.items
    assert result.items[0].stable_id != "ref"


def test_five_track_index_refuses_similarity_quality_claim() -> None:
    """[if] only five tracks are in the index [then] the report refuses to claim similarity quality, [else stop]."""
    report = semantic_search.build_retrieval_report("audio_reference", embedding_count=5, claim_count=3)
    assert report.similarity_quality_claimed is False
    assert str(semantic_search.SIMILARITY_QUALITY_MIN_TRACKS) in report.similarity_quality_note


def test_metadata_fts_search_unchanged(library: tuple[Path, Path]) -> None:
    """[if] metadata FTS search runs [then] title queries still resolve via search_index, [else stop]."""
    _data_dir, state_path = library
    search_index.ensure_index(state_path)
    hits, total = search_index.search_metadata_hits(state_path, "Artist", cap=10)
    assert total >= 1
    assert any("ref" == h.stable_id or h.title == "ref" for h in hits)
