"""PSSI phrases survive switching waveform or beatgrid to own (NATIVE-06).

[if] waveform or beatgrid is switched to own [then] real rekordbox PSSI phrases keep being served, [else stop].

Phrases are produced by ``_phrases_payload`` reading a PSSI-shaped tag, then
must survive ``apply_own_overlays`` with beatgrid=own, waveform=own, or both.

-Claude
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.adapters.rekordbox.paths import empty_anlz_payload
from apps.analysis import selection
from apps.analysis.lanes import LaneResult
from apps.analysis.store import open_conn, upsert_record
from apps.webui.server.app import create_app
from apps.webui.server.rb_vendor_pkg import anlz as anlz_mod
from apps.webui.server.rb_vendor_pkg import own_beatgrid_overlay as beatgrid_overlay_mod
from apps.webui.server.rb_vendor_pkg import own_overlays
from apps.webui.server.rb_vendor_pkg import own_waveform_overlay as waveform_overlay_mod
from tests.analysis_contract.conftest import own_record
from tests.fixtures.conftest import requires_fixture, resolve_required_fixture

pytestmark = pytest.mark.requirement("NATIVE-06")

STABLE_ID = "a" * 40
NOT_DECODED_REASON = "not_decoded: ffmpeg not on PATH"


@pytest.fixture(autouse=True)
def _launch_state_toggles() -> Any:
    selection.reset_toggles()
    yield
    selection.reset_toggles()


@pytest.fixture
def state_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    open_conn(db_path).close()
    monkeypatch.setattr(rb_config, "STATE_DB", db_path)
    return db_path


def _pssi_phrases() -> tuple[list[dict[str, Any]], list[float]]:
    times = [round(index * 0.5, 3) for index in range(16)]
    entry_a = SimpleNamespace(beat=1, kind=1)
    entry_b = SimpleNamespace(beat=5, kind=2)
    content = SimpleNamespace(mood=2, end_beat=9, entries=[entry_a, entry_b])
    pssi = SimpleNamespace(content=content)
    phrases = anlz_mod._phrases_payload({"PSSI": pssi}, times)
    assert phrases
    assert {key for row in phrases for key in row} >= {
        "start_s", "end_s", "kind", "mood",
    }
    return phrases, times


def _rekordbox_waveform_stub() -> dict[str, Any]:
    return {
        "kind": "mono",
        "preview": {"length": 2, "low": [9, 9], "mid": [8, 8], "high": [7, 7]},
        "detail": {"length": 2, "low": [6, 6], "mid": [5, 5], "high": [4, 4]},
    }


def _payload_with_pssi_phrases(phrases: list[dict[str, Any]]) -> dict[str, Any]:
    payload = empty_anlz_payload(STABLE_ID, 400)
    grid = anlz_mod._beatgrid_payload({})[0]
    grid["beats"] = [
        {"n": (index % 4) + 1, "bpm": 125.0, "t": round(0.48 * index, 3)}
        for index in range(64)
    ]
    grid["beat_count"] = len(grid["beats"])
    payload["beatgrid"] = grid
    payload["waveform"] = _rekordbox_waveform_stub()
    payload["phrases"] = phrases
    return payload


def _own_waveform_payload() -> dict[str, Any]:
    return {
        "kind": "tri",
        "preview": {"length": 3, "low": [1, 2, 3], "mid": [4, 5, 6], "high": [7, 8, 9]},
        "detail": {"length": 2, "low": [10, 11], "mid": [12, 13], "high": [14, 15]},
    }


def _write_own_beatgrid(state_db: Path) -> None:
    upsert_record(own_record(stable_id=STABLE_ID, lane="beatgrid"), db_path=state_db)


def _write_own_waveform(state_db: Path, *, failed: bool = False) -> None:
    if failed:
        result = LaneResult(status="failed", reason=NOT_DECODED_REASON)
    else:
        result = LaneResult(status="ok", payload=_own_waveform_payload())
    upsert_record(
        own_record(stable_id=STABLE_ID, lane="waveform", result=result),
        db_path=state_db,
    )


def test_phrases_from_pssi_tag_are_non_empty() -> None:
    phrases, _times = _pssi_phrases()
    assert len(phrases) > 0


def test_beatgrid_own_keeps_pssi_phrases(state_db: Path) -> None:
    phrases, _times = _pssi_phrases()
    _write_own_beatgrid(state_db)
    payload = _payload_with_pssi_phrases(phrases)
    selection.set_toggle("beatgrid", "own")

    served = own_overlays.apply_own_overlays(payload, STABLE_ID, state_db)

    assert served["phrases"] == phrases
    assert len(served["phrases"]) > 0
    assert served["beatgrid"]["source"] == "own"


def test_waveform_own_keeps_pssi_phrases(state_db: Path) -> None:
    phrases, _times = _pssi_phrases()
    _write_own_waveform(state_db)
    payload = _payload_with_pssi_phrases(phrases)
    rekordbox_waveform = payload["waveform"]
    selection.set_toggle("waveform", "own")

    served = own_overlays.apply_own_overlays(payload, STABLE_ID, state_db)

    assert served["phrases"] == phrases
    assert len(served["phrases"]) > 0
    assert served["waveform"]["kind"] == "tri"
    assert served["waveform"]["preview"] == _own_waveform_payload()["preview"]
    assert served["waveform"]["detail"] == _own_waveform_payload()["detail"]
    assert served["waveform"] != rekordbox_waveform


def test_both_lanes_own_keep_pssi_phrases(state_db: Path) -> None:
    phrases, _times = _pssi_phrases()
    _write_own_beatgrid(state_db)
    _write_own_waveform(state_db)
    payload = _payload_with_pssi_phrases(phrases)
    selection.set_toggle("beatgrid", "own")
    selection.set_toggle("waveform", "own")

    served = own_overlays.apply_own_overlays(payload, STABLE_ID, state_db)

    assert served["phrases"] == phrases
    assert len(served["phrases"]) > 0
    assert served["beatgrid"]["source"] == "own"
    assert served["waveform"]["kind"] == "tri"
    assert served["waveform"]["preview"]["low"] == [1, 2, 3]


def test_waveform_not_decoded_keeps_pssi_phrases(state_db: Path) -> None:
    phrases, _times = _pssi_phrases()
    _write_own_waveform(state_db, failed=True)
    payload = _payload_with_pssi_phrases(phrases)
    selection.set_toggle("waveform", "own")

    served = own_overlays.apply_own_overlays(payload, STABLE_ID, state_db)

    assert served["phrases"] == phrases
    assert served["waveform"]["status"] == "failed"
    assert "not_decoded" in served["waveform"]["reason"]
    assert served["waveform"]["preview"] == {"length": 0, "low": [], "mid": [], "high": []}
    assert served["waveform"]["detail"] == {"length": 0, "low": [], "mid": [], "high": []}


def test_overlays_never_assign_phrases() -> None:
    for module in (beatgrid_overlay_mod, waveform_overlay_mod):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert 'payload["phrases"]' not in source
        assert "payload['phrases']" not in source


def test_waveform_lane_is_registered_for_serving(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("MDT_DATA_DIR", str(tmp_path))
    app = create_app()
    app.state.analysis_db_path = db_path
    client = TestClient(app)

    body = client.get("/api/v1/analysis/source").json()
    assert "waveform" in body["serving"]
    res = client.put(
        "/api/v1/analysis/source",
        json={"lane": "waveform", "toggle": "own"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["lanes"]["waveform"]["toggle"] == "own"


@requires_fixture("rb-usb-export")
def test_usb_export_pssi_phrases_survive_both_lanes_own(state_db: Path) -> None:
    root = resolve_required_fixture("rb-usb-export")
    directory = root / "PIONEER" / "USBANLZ" / "P063" / "00016827"
    tags, _unreadable = anlz_mod._first_tags(directory)
    if tags.get("PSSI") is None:
        pytest.skip("fixture ANLZ dir has no PSSI tag")
    grid, times = anlz_mod._beatgrid_payload(tags)
    phrases = anlz_mod._phrases_payload(tags, times)
    if not phrases:
        pytest.skip("fixture PSSI tag produced no phrases")
    _write_own_beatgrid(state_db)
    _write_own_waveform(state_db)
    payload = empty_anlz_payload(STABLE_ID, 400)
    payload["beatgrid"] = grid
    payload["waveform"] = _rekordbox_waveform_stub()
    payload["phrases"] = phrases
    selection.set_toggle("beatgrid", "own")
    selection.set_toggle("waveform", "own")

    served = own_overlays.apply_own_overlays(payload, STABLE_ID, state_db)

    assert served["phrases"] == phrases
