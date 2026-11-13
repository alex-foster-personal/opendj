"""Tests for tools/deck-diagrams/scripts/catalog_status.py (fixture devices dir, no network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[2] / "tools" / "deck-diagrams" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import catalog_status  # noqa: E402

_MIDI = {"controls": [{"name": "play", "notes": "Play [D1]"}]}
_LAYOUT_OK = {"device": "full", "controls": [{"fig": "D1", "midi": {"name": "play"}}]}
_LAYOUT_BAD = {"device": "bad", "controls": [{"fig": "D2", "midi": {"name": "nope"}}]}


def _entry(dev_id: str) -> dict[str, Any]:
    """Build one catalog row with stale status and an unrelated field."""
    return {
        "id": dev_id,
        "model": dev_id.upper(),
        "status": {"docs": "ok", "midi": "stale", "diagram": "stub", "checksum": "n/a", "hover": "ok"},
    }


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Write a devices dir (full, bad checksum, midi-only, empty) and a catalog."""
    devices = tmp_path / "devices"
    for dev_id, files in {
        "full": {"midi.json": _MIDI, "layout.json": _LAYOUT_OK, "deck.html": None},
        "bad": {"midi.json": _MIDI, "layout.json": _LAYOUT_BAD, "deck.html": None},
        "midionly": {"midi.json": _MIDI},
    }.items():
        d = devices / dev_id
        d.mkdir(parents=True)
        for name, body in files.items():
            (d / name).write_text("<html></html>" if body is None else json.dumps(body))
    catalog = tmp_path / "controllers.json"
    doc = {"version": 1, "controllers": [_entry(i) for i in ("full", "bad", "midionly", "absent")], "summary": {"x": 1}}
    catalog.write_text(json.dumps(doc, indent=2) + "\n")
    return devices, catalog


def test_derive_status_full_device_is_ok(tmp_path: Path) -> None:
    """A device with midi.json, layout.json and deck.html reports diagram ok and checksum ok."""
    devices, _ = _fixture(tmp_path)
    assert catalog_status.derive_status(devices / "full") == {"midi": "ok", "diagram": "ok", "checksum": "ok"}


def test_derive_status_partial_and_failing(tmp_path: Path) -> None:
    """Checksum fails on a bad layout; missing files report pending."""
    devices, _ = _fixture(tmp_path)
    assert catalog_status.derive_status(devices / "bad")["checksum"] == "fail"
    assert catalog_status.derive_status(devices / "midionly") == {
        "midi": "ok",
        "diagram": "pending",
        "checksum": "pending",
    }
    assert catalog_status.derive_status(devices / "absent") == {
        "midi": "pending",
        "diagram": "pending",
        "checksum": "pending",
    }


def test_check_fails_on_drift_then_passes_after_write(tmp_path: Path) -> None:
    """--check exits 1 while the catalog disagrees with disk and 0 after --write."""
    devices, catalog = _fixture(tmp_path)
    args = ["--catalog", str(catalog), "--devices", str(devices)]
    assert catalog_status.main(["--check", *args]) == 1
    assert catalog_status.main(["--write", *args]) == 0
    assert catalog_status.main(["--check", *args]) == 0


def test_write_touches_only_derived_status_fields(tmp_path: Path) -> None:
    """--write changes status.midi/diagram/checksum and nothing else."""
    devices, catalog = _fixture(tmp_path)
    before = json.loads(catalog.read_text())
    catalog_status.main(["--write", "--catalog", str(catalog), "--devices", str(devices)])
    after = json.loads(catalog.read_text())
    for row in (*before["controllers"], *after["controllers"]):
        for key in catalog_status.DERIVED_KEYS:
            row["status"].pop(key)
    assert after == before
