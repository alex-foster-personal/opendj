"""POST /api/v1/copilot/suggest-next route tests (gating-wave unit).

Regression lines (CLAUDE.md format):
  * if suggest-next for a known track doesn't return ranked candidates
    excluding the current track then broken
  * if a manual pairing current->cand doesn't surface pair_manual in
    rationale_tags then broken
  * if an unknown stable_id / session id isn't an explicit 404 naming it
    then broken
  * if a current track missing bpm/key isn't a 422 insufficient_data
    naming the missing fields then broken
  * if a library with no in-window candidates doesn't return an explicit
    empty candidates list then broken
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Pairing, Provenance, Track
from apps.webui.server.routes import copilot as copilot_routes

BASE = "/api/v1/copilot/suggest-next"
PEAK_BASE = "/api/v1/copilot/peak-pressure"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _seed_track(
    backend: InMemoryBackend,
    stable_id: str,
    *,
    title: str,
    artist: str,
    bpm: float | None,
    key: str | None,
    energy: int | None = None,
) -> None:
    created = _iso(datetime(2026, 7, 22, 10, 0, 0, tzinfo=UTC))
    provenance = {}
    if energy is not None:
        provenance["energy"] = Provenance(
            value=energy, source="mik", confidence=1.0, modified_at=created,
            status="ok",
        )
    backend.seed_track(Track(
        stable_id=stable_id, title=title, artist=artist, bpm=bpm, key=key,
        created_at=created, updated_at=created, provenance=provenance,
    ))


@pytest.fixture
def backend() -> InMemoryBackend:
    """Current 124bpm/8A plus in-window + out-of-window candidates."""
    b = InMemoryBackend()
    _seed_track(b, "cur-001", title="Midnight Drive", artist="the maintainer",
                bpm=124.0, key="8A", energy=6)
    # In BPM window (6%): 128/124 = +3.2%, 120/124 = -3.2%, 118 = -4.8%.
    _seed_track(b, "cand-128", title="Oxide", artist="Beta",
                bpm=128.0, key="8A", energy=7)
    _seed_track(b, "cand-120", title="Gulf", artist="Gamma",
                bpm=120.0, key="9A", energy=5)
    _seed_track(b, "cand-118", title="Nest", artist="Epsilon",
                bpm=118.0, key="7A", energy=6)
    # Out of window: 140/124 = +12.9% -> engine must drop it.
    _seed_track(b, "cand-140", title="Phoenix", artist="Delta",
                bpm=140.0, key="8A", energy=9)
    return b


@pytest.fixture
def client(backend: InMemoryBackend) -> Iterator[TestClient]:
    app = create_app(
        backend=backend, bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None, syncthing_status_fn=lambda: None,
    )
    # app.py is integrator-owned (hotspot): wire the router here exactly as
    # the integrator will -- app.include_router(..., prefix="/api/v1").
    app.include_router(copilot_routes.router, prefix="/api/v1")
    with TestClient(app) as c:
        yield c


# ----------------------------------------------------------- happy path


@pytest.mark.requirement("CAT-05")
def test_suggest_next_returns_ranked_candidates(client: TestClient) -> None:
    r = client.post(BASE, json={"stable_id": "cur-001"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current"]["stable_id"] == "cur-001"
    assert body["current"]["key_camelot"] == "8A"
    assert body["current"]["energy"] == 6
    assert body["context_source"] == "empty"
    assert body["context_size"] == 0
    assert body["pressure"]["cue"] == "unknown"
    assert body["pressure"]["scored_tracks"] == 0

    ids = [c["stable_id"] for c in body["candidates"]]
    assert "cur-001" not in ids, "current track must never suggest itself"
    assert "cand-140" not in ids, "outside the 6% BPM window"
    assert set(ids) == {"cand-128", "cand-120", "cand-118"}

    scores = [c["score"] for c in body["candidates"]]
    assert scores == sorted(scores, reverse=True), "score DESC contract"
    for cand in body["candidates"]:
        assert isinstance(cand["rationale_tags"], list)
        assert "bpm" in cand["rationale_numbers"]
        assert cand["title"] is not None


@pytest.mark.requirement("CAT-05")
def test_same_key_candidate_gets_camelot_tag(client: TestClient) -> None:
    r = client.post(BASE, json={"stable_id": "cur-001"})
    assert r.status_code == 200
    by_id = {c["stable_id"]: c for c in r.json()["candidates"]}
    assert "camelot_step_0" in by_id["cand-128"]["rationale_tags"]


@pytest.mark.requirement("CAT-05")
def test_pairing_bumps_and_tags_candidate(
    client: TestClient, backend: InMemoryBackend
) -> None:
    created = _iso(datetime(2026, 7, 22, 10, 0, 0, tzinfo=UTC))
    backend.seed_pairing(Pairing(
        pairing_id="p-001", from_stable_id="cur-001",
        to_stable_id="cand-120", direction="->", source="manual",
        notes=None, created_at=created, updated_at=created,
    ))
    r = client.post(BASE, json={"stable_id": "cur-001"})
    assert r.status_code == 200
    by_id = {c["stable_id"]: c for c in r.json()["candidates"]}
    assert "pair_manual" in by_id["cand-120"]["rationale_tags"]


@pytest.mark.requirement("CAT-05")
def test_session_ids_drive_artist_cooldown(client: TestClient) -> None:
    # Beta (cand-128's artist) just played -> cooldown must drop cand-128.
    r = client.post(
        BASE, json={"stable_id": "cur-001", "session_ids": ["cand-128"]}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["context_source"] == "manual"
    assert body["context_size"] == 1
    ids = [c["stable_id"] for c in body["candidates"]]
    assert "cand-128" not in ids, "artist cooldown must apply"
    assert "cand-120" in ids


@pytest.mark.requirement("CAT-05")
def test_top_n_truncates(client: TestClient) -> None:
    r = client.post(BASE, json={"stable_id": "cur-001", "top_n": 1})
    assert r.status_code == 200
    assert len(r.json()["candidates"]) == 1


# ----------------------------------------------------------- explicit states


@pytest.mark.requirement("CAT-05")
def test_unknown_stable_id_is_404(client: TestClient) -> None:
    r = client.post(BASE, json={"stable_id": "nope-999"})
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "not_found"
    assert "nope-999" in body["message"]


@pytest.mark.requirement("CAT-05")
def test_unknown_session_id_is_404_naming_it(client: TestClient) -> None:
    r = client.post(
        BASE, json={"stable_id": "cur-001", "session_ids": ["ghost-1"]}
    )
    assert r.status_code == 404
    assert "ghost-1" in r.json()["message"]


@pytest.mark.requirement("CAT-05")
def test_missing_bpm_and_key_is_422_naming_fields(
    client: TestClient, backend: InMemoryBackend
) -> None:
    _seed_track(backend, "raw-001", title="Unanalyzed", artist="Zeta",
                bpm=None, key=None, energy=None)
    r = client.post(BASE, json={"stable_id": "raw-001"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "insufficient_data"
    missing = body["details"]["missing"]
    assert missing["bpm"] == ["raw-001"]
    assert missing["key"] == ["raw-001"]
    assert missing["energy"] == ["raw-001"]
    assert body["details"]["stable_id"] == "raw-001"


@pytest.mark.requirement("CAT-05")
def test_unparseable_key_counts_as_missing(
    client: TestClient, backend: InMemoryBackend
) -> None:
    _seed_track(backend, "oddkey-1", title="Odd", artist="Eta",
                bpm=126.0, key="not-a-key", energy=5)
    r = client.post(BASE, json={"stable_id": "oddkey-1"})
    assert r.status_code == 422
    assert r.json()["details"]["missing"]["key"] == ["oddkey-1"]


@pytest.mark.requirement("CAT-05")
def test_missing_energy_alone_does_not_422(
    client: TestClient, backend: InMemoryBackend
) -> None:
    # Sqlite backend does not project energy yet (documented gap) -- the
    # endpoint must still work, scoring energy as neutral.
    _seed_track(backend, "noeng-1", title="NoEnergy", artist="Theta",
                bpm=125.0, key="8A", energy=None)
    r = client.post(BASE, json={"stable_id": "noeng-1"})
    assert r.status_code == 200
    assert r.json()["current"]["energy"] is None


@pytest.mark.requirement("CAT-05")
def test_no_in_window_candidates_is_explicit_empty(client: TestClient) -> None:
    # cand-140 at 140bpm: every other track is >6% away -> empty list.
    r = client.post(BASE, json={"stable_id": "cand-140"})
    assert r.status_code == 200
    assert r.json()["candidates"] == []


@pytest.mark.requirement("CAT-05")
def test_standard_key_notation_is_normalised(
    client: TestClient, backend: InMemoryBackend
) -> None:
    # "Am" is Camelot 8A -- current key must be normalised, not echoed raw.
    _seed_track(backend, "am-001", title="Classic", artist="Iota",
                bpm=124.0, key="Am", energy=6)
    r = client.post(BASE, json={"stable_id": "am-001"})
    assert r.status_code == 200
    assert r.json()["current"]["key_camelot"] == "8A"


@pytest.mark.requirement("AI-05")
def test_peak_pressure_route_release_after_four_high_energy(
    client: TestClient, backend: InMemoryBackend
) -> None:
    for i in range(4):
        _seed_track(
            backend, f"peak-{i}", title=f"Peak {i}", artist=f"P{i}",
            bpm=124.0, key="8A", energy=8,
        )
    r = client.post(
        PEAK_BASE,
        json={"session_ids": ["peak-0", "peak-1", "peak-2", "peak-3"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cue"] == "release"
    assert body["limitation"] == "Metadata energy is not crowd response."


@pytest.mark.requirement("AI-05")
def test_peak_pressure_unknown_session_is_404(client: TestClient) -> None:
    r = client.post(PEAK_BASE, json={"session_ids": ["ghost-peak"]})
    assert r.status_code == 404
    assert "ghost-peak" in r.json()["message"]


@pytest.mark.requirement("AI-05")
def test_suggest_next_includes_pressure_and_release_prior(
    client: TestClient, backend: InMemoryBackend
) -> None:
    for i in range(4):
        _seed_track(
            backend, f"peak-{i}", title=f"Peak {i}", artist=f"P{i}",
            bpm=124.0, key="8A", energy=8,
        )
    _seed_track(
        backend, "cand-126", title="High", artist="High",
        bpm=126.0, key="8A", energy=9,
    )
    r = client.post(
        BASE,
        json={
            "stable_id": "cur-001",
            "session_ids": ["peak-0", "peak-1", "peak-2", "peak-3"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pressure"]["cue"] == "release"
    tags = [t for c in body["candidates"] for t in c["rationale_tags"]]
    assert "release_a_little" in tags
    high_energy = [
        c for c in body["candidates"]
        if c.get("energy") is not None and c["energy"] >= 8
    ]
    assert high_energy, "high-energy candidates must remain listed"


@pytest.mark.requirement("AI-05")
def test_copilot_route_has_no_camera_import() -> None:
    import re
    from pathlib import Path

    source = Path(copilot_routes.__file__).read_text(encoding="utf-8")
    assert re.search(r"(?i)camera|cv2|webcam", source) is None
