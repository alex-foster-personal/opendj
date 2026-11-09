"""requirement: SKIN-10481

if panel.json or panel.reference.json drifts from build_ddj1000_panel.py output then broken
if the shipped DDJ-1000 pair fails control_position_error thresholds, or scores exactly zero then broken
if the reference is not a measurement (measured true, px box per control, mm within 0.06 of px) then broken
if a DDJ-1000 layout file fails its JSON Schema, leaves the panel, or repeats an id then broken
if a 20 mm shift of d1-play-pause still passes control_position_error then broken
if panel.json breaks left-right or above-below order from the reference then broken
if the right deck is not a translation of the left deck (same relative order, same sizes) then broken
if GET /api/v1/practice/devices does not list ddj-1000 or serve its 120 controls then broken
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import jsonschema
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.routes import practice
from scripts.skins import control_position_error

ROOT = Path(__file__).resolve().parents[2]
DEVICE_DIR = ROOT / "tools" / "deck-diagrams" / "devices" / "ddj-1000"
PANEL = DEVICE_DIR / "panel.json"
REFERENCE = DEVICE_DIR / "panel.reference.json"
BUILDER = ROOT / "tools" / "deck-diagrams" / "scripts" / "build_ddj1000_panel.py"
SCHEMAS = ROOT / "tools" / "deck-diagrams" / "schema"
CONTROL_COUNT = 120


def load_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_ddj1000_panel", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _panel() -> dict:
    return json.loads(PANEL.read_text())


def _reference() -> dict:
    return json.loads(REFERENCE.read_text())


def _centres_mm(doc: dict) -> dict[str, tuple[float, float]]:
    return {c["id"]: (c["x"] + c["w"] / 2, c["y"] + c["h"] / 2) for c in doc["controls"]}


def _overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return not (a1 < b0 or b1 < a0)


def _neighbour_pairs(doc: dict) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    by_id = {c["id"]: c for c in doc["controls"]}
    ids = list(by_id)
    lr: list[tuple[str, str]] = []
    ab: list[tuple[str, str]] = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            ca, cb = by_id[ids[i]], by_id[ids[j]]
            cxa, cya = ca["x"] + ca["w"] / 2, ca["y"] + ca["h"] / 2
            cxb, cyb = cb["x"] + cb["w"] / 2, cb["y"] + cb["h"] / 2
            if _overlap(ca["y"], ca["y"] + ca["h"], cb["y"], cb["y"] + cb["h"]) and abs(cxa - cxb) >= 5.0:
                lr.append((ids[i], ids[j]) if cxa < cxb else (ids[j], ids[i]))
            if _overlap(ca["x"], ca["x"] + ca["w"], cb["x"], cb["x"] + cb["w"]) and abs(cya - cyb) >= 5.0:
                ab.append((ids[i], ids[j]) if cya < cyb else (ids[j], ids[i]))
    return lr, ab


def _order_violations(candidate: dict, pairs: tuple[list[tuple[str, str]], list[tuple[str, str]]]) -> list[str]:
    lr, ab = pairs
    cen = _centres_mm(candidate)
    out = [f"lr {a} {b}" for a, b in lr if cen[a][0] >= cen[b][0]]
    out += [f"ab {a} {b}" for a, b in ab if cen[a][1] >= cen[b][1]]
    return out


# ----------------------------------------------------------------- builder drift


def test_panel_pair_matches_builder() -> None:
    builder = load_builder()
    assert json.loads(json.dumps(builder.build())) == _panel()
    assert json.loads(json.dumps(builder.build_reference())) == _reference()


def test_layout_files_validate_and_stay_inside_the_panel() -> None:
    panel_schema = json.loads((SCHEMAS / "panel-layout.schema.json").read_text())
    ref_schema = json.loads((SCHEMAS / "panel-reference.schema.json").read_text())
    panel_doc, ref = _panel(), _reference()
    jsonschema.Draft202012Validator(panel_schema).validate(panel_doc)
    jsonschema.Draft202012Validator(ref_schema).validate(ref)
    ids = [c["id"] for c in panel_doc["controls"]]
    assert len(ids) == len(set(ids)) == CONTROL_COUNT
    assert "measured" not in panel_doc
    w, d = panel_doc["panel"]["w"], panel_doc["panel"]["d"]
    for c in panel_doc["controls"]:
        assert c["x"] >= 0 and c["y"] >= 0 and c["x"] + c["w"] <= w and c["y"] + c["h"] <= d, c["id"]


# ----------------------------------------------------------------- position error


def test_shipped_measured_pair_passes(capsys) -> None:  # type: ignore[no-untyped-def]
    assert control_position_error.main([str(PANEL), str(REFERENCE)]) == 0
    assert capsys.readouterr().out.startswith("[PASS]")
    report = control_position_error.measure(_panel(), _reference())
    assert report.matched == CONTROL_COUNT
    assert report.p95_px is not None and report.max_px is not None
    assert report.p95_px <= 8 and report.max_px <= 15
    assert report.max_px > 0  # the renderer normalisation moves centres, so a copy cannot pass


def test_reference_is_a_measurement() -> None:
    panel_doc, ref = _panel(), _reference()
    assert ref["measured"] is True
    assert ref["panel"] == panel_doc["panel"]
    assert [c["id"] for c in ref["controls"]] == [c["id"] for c in panel_doc["controls"]]
    trace = ref["trace"]
    assert trace["source_pdf_sha256"] and trace["source_image_sha256"] and trace["repeat_spread_mm"] > 0
    assert trace["mm_per_px"]["x"] != trace["mm_per_px"]["y"], "the artwork is anisotropic; one scale would be wrong"
    sx, sy = trace["mm_per_px"]["x"], trace["mm_per_px"]["y"]
    x0_plate, y0_plate = trace["panel_px"]["x0"], trace["panel_px"]["y0"]
    for row in ref["controls"]:
        x0, y0, x1, y1 = row["px"]
        assert abs(row["x"] - (x0 - x0_plate) * sx) <= 0.06, row["id"]
        assert abs(row["y"] - (y0 - y0_plate) * sy) <= 0.06, row["id"]
        assert abs(row["w"] - (x1 - x0 + 1) * sx) <= 0.06, row["id"]
        assert abs(row["h"] - (y1 - y0 + 1) * sy) <= 0.06, row["id"]


def test_negative_control_moved_play_pause_fails(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    cand = _panel()
    row = next(r for r in cand["controls"] if r["id"] == "d1-play-pause")
    row["x"] += 20.0
    cand_path = tmp_path / "panel.json"
    cand_path.write_text(json.dumps(cand), encoding="utf-8")
    assert control_position_error.main([str(cand_path), str(REFERENCE)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out and "d1-play-pause" in out


def test_relative_order_matches_reference() -> None:
    pairs = _neighbour_pairs(_reference())
    lr, ab = pairs
    assert len(lr) + len(ab) >= 200
    assert ("d1-loop-in", "d1-loop-out") in lr
    assert ("d1-cue", "d1-play-pause") in ab
    assert ("ch1-hi", "ch1-mid") in ab
    shipped = _panel()
    assert not _order_violations(shipped, pairs)
    swapped = copy.deepcopy(shipped)
    a = next(r for r in swapped["controls"] if r["id"] == "d1-loop-in")
    b = next(r for r in swapped["controls"] if r["id"] == "d1-loop-out")
    a["x"], b["x"] = b["x"], a["x"]
    assert any("d1-loop-in" in v and "d1-loop-out" in v for v in _order_violations(swapped, pairs))


def test_right_deck_is_a_translation_of_the_left_deck() -> None:
    by_id = {c["id"]: c for c in _panel()["controls"]}
    left = {k[3:]: v for k, v in by_id.items() if k.startswith("d1-")}
    right = {k[3:]: v for k, v in by_id.items() if k.startswith("d2-")}
    assert set(left) == set(right) and len(left) == 36
    shifts = [right[k]["x"] - left[k]["x"] for k in left]
    assert 300 <= min(shifts) and max(shifts) - min(shifts) <= 4.0, shifts
    for k in left:
        assert (left[k]["w"], left[k]["h"]) == (right[k]["w"], right[k]["h"]), k
        assert abs(left[k]["y"] - right[k]["y"]) <= 2.0, k


# ----------------------------------------------------------------- route


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(practice.router, prefix="/api/v1")
    return TestClient(app)


def test_route_lists_and_serves_ddj1000(client: TestClient) -> None:
    assert "ddj-1000" in client.get("/api/v1/practice/devices").json()["devices"]
    r = client.get("/api/v1/practice/devices/ddj-1000")
    assert r.status_code == 200
    doc = r.json()
    assert doc["format"] == "opendj-panel-layout/1"
    assert doc["device_map"] == "DDJ1000_MAP"
    assert len(doc["controls"]) == CONTROL_COUNT


def test_payload_stages_ddj1000_panel(tmp_path: Path) -> None:
    from scripts.build_engine_payload import _stage_practice_panels

    staged = _stage_practice_panels(ROOT, tmp_path / "app")
    assert "tools/deck-diagrams/devices/ddj-1000/panel.json" in staged
