"""The PARITY-02 Phrase row: rbx keeps STRUCT-02, own serves only own (STRUCT-04).

[if] the phrase lane is set to own [then] /anlz serves only own sections, never rekordbox's, [else stop].

The control for every own-lane assertion is the same input on the rbx lane,
so a test that passes because the lane is ignored cannot pass both ways.

[if] the Phrase row picks own [then] /anlz serves only own sections, [else stop].

-Claude
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.analysis import selection
from apps.analysis_structure import pipeline
from apps.analysis_structure.pipeline import build_sidecar, write_sidecar
from apps.analysis_structure.serve import merge_own_phrases

pytestmark = pytest.mark.requirement("STRUCT-04")

GRID = [2.0 * i for i in range(120)]
GRID_ID = {"backend": "own_beatgrid.backfill", "backend_version": "1.0.0"}
RAW_OK = {
    "status": "ok",
    "segments": [
        {"start": 0.0, "end": 32.3, "label": "intro"},
        {"start": 32.3, "end": 96.1, "label": "chorus"},
        {"start": 96.1, "end": 180.0, "label": "outro"},
    ],
    "versions": {"all-in-one-mps": "1.0.0", "device": "cpu"},
}
PSSI = [{"start_s": 0.0, "end_s": 30.0, "kind": 1, "mood": 2}]


@pytest.fixture(autouse=True)
def _launch_state_toggles() -> Iterator[None]:
    selection.reset_toggles()
    yield
    selection.reset_toggles()


def _with_sidecar(tmp_path: Path) -> Path:
    write_sidecar(tmp_path, build_sidecar("s1", RAW_OK, GRID, GRID_ID))
    return tmp_path


def test_own_serves_own_sections_even_where_pssi_exists(tmp_path: Path) -> None:
    data = _with_sidecar(tmp_path)
    own = merge_own_phrases({"phrases": PSSI}, "s1", data, source="own")
    assert own["phrases_source"] == "own"
    assert [p["start_s"] for p in own["phrases"]] == [0.0, 32.0, 96.0]
    assert all(p["source"] == "own" for p in own["phrases"])
    # Control: the same payload on rbx keeps rekordbox (STRUCT-02 unchanged).
    rbx = merge_own_phrases({"phrases": PSSI}, "s1", data, source="rbx")
    assert rbx["phrases"] == PSSI and rbx["phrases_source"] == "rekordbox"


def test_own_with_no_sidecar_is_none_never_rekordbox(tmp_path: Path) -> None:
    out = merge_own_phrases({"phrases": PSSI}, "absent", tmp_path, source="own")
    assert out["phrases"] == [] and out["phrases_source"] == "none"


def test_own_with_a_failed_sidecar_reports_it_and_serves_nothing(tmp_path: Path) -> None:
    write_sidecar(tmp_path, pipeline._failed("s2", "audio_not_local"))
    out = merge_own_phrases({"phrases": PSSI}, "s2", tmp_path, source="own")
    assert out["phrases"] == [] and out["phrases_source"] == "none"
    assert out["own_phrases"]["reason"] == "audio_not_local"


def test_rbx_still_fills_an_empty_pssi_from_own(tmp_path: Path) -> None:
    out = merge_own_phrases({"phrases": []}, "s1", _with_sidecar(tmp_path), source="rbx")
    assert out["phrases_source"] == "own"


def test_an_unknown_source_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown phrase source"):
        merge_own_phrases({"phrases": []}, "s1", tmp_path, source="djay")


def test_phrase_is_a_selection_lane_but_not_a_record_lane() -> None:
    from apps.analysis.lanes import LANES

    assert "phrase" in selection.SELECTION_LANES
    assert "phrase" not in LANES
    assert selection.check_lane("phrase") == "phrase"
    with pytest.raises(selection.SelectionError):
        selection.check_lane("genre")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[Any]:
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


def test_the_dropdown_endpoint_lists_serves_and_toggles_phrase(client: Any) -> None:
    body = client.get("/api/v1/analysis/source").json()
    assert body["lanes"]["phrase"]["effective"] == "rbx"
    assert "phrase" in body["serving"]
    put = client.put("/api/v1/analysis/source", json={"lane": "phrase", "toggle": "own"})
    assert put.status_code == 200
    assert put.json()["lanes"]["phrase"]["effective"] == "own"
    # Control: the other lanes did not move with it.
    assert put.json()["lanes"]["beatgrid"]["effective"] == "rbx"
