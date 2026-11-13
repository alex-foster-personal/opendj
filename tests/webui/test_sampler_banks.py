"""Classic sampler banks: GET and PUT /api/v1/sampler/banks over ``sampler-banks.json`` (#9994).

[if] the banks document is not served, stored, validated or marked as issue #9994 says [then] these fail, [else stop].

Regression one-liners:
  - if GET with no file does not answer v 1, active_bank 0, master_gain_db 0 and 4 x 16 empty slots then broken
  - if a PUT document does not read back the same from a fresh client then broken
  - if an unknown mode or a gain outside -24..+6 dB is not a 422, or stores anything, then broken
  - if a slot whose track is gone is dropped instead of kept with missing true then broken
  - if a PUT leaves a temp file beside sampler-banks.json then broken
  - if master_gain_db does not round-trip, or an old document without it does not read as 0 dB, then broken
  - if a master_gain_db outside -60..0 dB is not a 422, or stores anything, then broken
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from apps.shared.state import sampler_banks
from apps.webui.server.backend import StateBackend
from apps.webui.server.routes import sampler
from apps.webui.server.sampler_bank_models import BankMode
from apps.webui.server.sampler_sources import TrackMeta
from tests.webui.sampler_support import GRID, FakeSources, make_app, make_env

URL = "/api/v1/sampler/banks"
GONE = "c" * 40


class LibrarySources(FakeSources):
    """FakeSources whose meta resolves only the tracks still in the library (GONE was deleted)."""

    def meta(self, request: Request, backend: StateBackend, stable_ids: Sequence[str]) -> dict[str, TrackMeta]:
        """Listing rows for the asked tracks that still resolve."""
        return super().meta(request, backend, [sid for sid in stable_ids if sid != GONE])


def _app(state: Path, sources: FakeSources) -> FastAPI:
    app = make_app(state, sources)
    app.include_router(sampler.router, prefix="/api/v1")
    return app


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    for base in make_env(tmp_path, monkeypatch):
        sources = LibrarySources(base.sources.audio)
        with TestClient(_app(base.state, sources)) as client:
            yield SimpleNamespace(**{**vars(base), "sources": sources}, client=client)


def _doc(**slots: dict[str, Any]) -> dict[str, Any]:
    """A banks document with the given ``b<bank>s<slot>`` slots loaded, the rest empty."""
    doc = sampler_banks.default_document()
    for key, slot in slots.items():
        b, s = key[1:].split("s")
        doc["banks"][int(b)][int(s)] = slot
    return doc


def _track(sid: str = GRID, mode: str = "loop", gain_db: float = -3.0) -> dict[str, Any]:
    return {"stable_id": sid, "mode": mode, "gain_db": gain_db, "title": "Kick loop"}


def _file_path(env: SimpleNamespace) -> Path:
    return sampler_banks.banks_path(env.state)


def test_get_with_no_file_answers_default_empty_banks(env: SimpleNamespace) -> None:
    """[if] no banks were ever put [then] GET answers v 1, bank 0, master 0 dB and 4 x 16 empty slots, [else stop]."""
    r = env.client.get(URL)
    assert r.status_code == 200, r.text
    assert r.json() == {"v": 1, "active_bank": 0, "master_gain_db": 0.0, "banks": [[None] * 16 for _ in range(4)]}
    assert not _file_path(env).exists(), "a GET must never create the file"


def test_put_round_trips_through_a_fresh_client(env: SimpleNamespace) -> None:
    """[if] banks are put [then] a fresh client on the same state dir reads the same slots back, [else stop]."""
    doc = _doc(
        b0s0=_track(), b3s15={"path": "/music/one-shots/clap.wav", "mode": "gate", "gain_db": 6.0, "title": "Clap"}
    )
    doc["active_bank"] = 2
    put = env.client.put(URL, json=doc)
    assert put.status_code == 200, put.text
    with TestClient(_app(env.state, LibrarySources(env.sources.audio))) as fresh:
        got = fresh.get(URL).json()
    assert got["active_bank"] == 2
    assert got["banks"][0][0] == {**_track(), "missing": False}
    assert got["banks"][3][15] == {
        "path": "/music/one-shots/clap.wav",
        "mode": "gate",
        "gain_db": 6.0,
        "title": "Clap",
        "missing": False,
    }
    assert sum(s is not None for bank in got["banks"] for s in bank) == 2
    assert got == put.json()


@pytest.mark.parametrize(
    ("slot", "field"),
    [(_track(mode="reverse"), "mode"), (_track(gain_db=6.5), "gain_db"), (_track(gain_db=-24.1), "gain_db")],
)
def test_put_refuses_unknown_mode_or_bad_gain_and_stores_nothing(
    env: SimpleNamespace, slot: dict[str, Any], field: str
) -> None:
    """[if] a slot has an unknown mode or a gain outside -24..+6 dB [then] 422 and the stored document is unchanged, [else stop]."""
    first = _doc(b1s4=_track())
    assert env.client.put(URL, json=first).status_code == 200
    before = _file_path(env).read_bytes()
    r = env.client.put(URL, json=_doc(b0s0=slot))
    assert r.status_code == 422, r.text
    assert field in json.dumps(r.json()["detail"])
    assert _file_path(env).read_bytes() == before
    assert env.client.get(URL).json()["banks"][1][4]["stable_id"] == GRID


def test_store_refuses_what_the_route_refuses(tmp_path: Path) -> None:
    """[if] the store is handed an unknown mode or out-of-range gain directly [then] ValueError and no file, [else stop]."""
    state = tmp_path / "state.db"
    for slot in (_track(mode="reverse"), _track(gain_db=7.0)):
        with pytest.raises(sampler_banks.InvalidBanksDocument):
            sampler_banks.write_banks(state, _doc(b0s0=slot))
    assert not sampler_banks.banks_path(state).exists()
    assert set(get_args(BankMode)) == set(sampler_banks.MODES)


def test_slot_whose_track_is_gone_is_kept_marked_missing(env: SimpleNamespace) -> None:
    """[if] a slot's stable_id no longer resolves in the library [then] GET keeps it with missing true, [else stop]."""
    assert env.client.put(URL, json=_doc(b0s0=_track(), b0s1=_track(GONE, mode="one_shot"))).status_code == 200
    bank = env.client.get(URL).json()["banks"][0]
    assert bank[0]["missing"] is False
    assert bank[1] == {**_track(GONE, mode="one_shot"), "missing": True}
    stored = json.loads(_file_path(env).read_text(encoding="utf-8"))
    assert "missing" not in stored["banks"][0][1], "missing is derived on read, never stored"


def test_put_is_atomic_and_leaves_no_temp_file(env: SimpleNamespace) -> None:
    """[if] banks are put twice [then] only sampler-banks.json sits in the state dir beside its own files, [else stop]."""
    before = {p.name for p in env.state.parent.iterdir()}
    for gain in (0.0, -12.0):
        assert env.client.put(URL, json=_doc(b2s7=_track(gain_db=gain))).status_code == 200
    after = {p.name for p in env.state.parent.iterdir()} - before
    assert after == {sampler_banks.FILENAME}, after
    assert json.loads(_file_path(env).read_text(encoding="utf-8"))["banks"][2][7]["gain_db"] == -12.0


# ---------- master gain (#9989) ------------------------------------------------------


def test_master_gain_round_trips_through_a_fresh_client(env: SimpleNamespace) -> None:
    """[if] a master_gain_db is put [then] a fresh client reads it back and the file stores it, [else stop]."""
    doc = {**_doc(b0s0=_track()), "master_gain_db": -18.5}
    put = env.client.put(URL, json=doc)
    assert put.status_code == 200, put.text
    assert put.json()["master_gain_db"] == -18.5
    with TestClient(_app(env.state, LibrarySources(env.sources.audio))) as fresh:
        assert fresh.get(URL).json()["master_gain_db"] == -18.5
    assert json.loads(_file_path(env).read_text(encoding="utf-8"))["master_gain_db"] == -18.5


def test_old_document_without_master_gain_reads_as_zero_and_is_then_written(env: SimpleNamespace) -> None:
    """[if] a stored doc predates master_gain_db [then] GET says 0.0 and the next PUT stores it explicitly, [else stop]."""
    old = _doc(b1s2=_track())
    del old["master_gain_db"]
    _file_path(env).write_text(json.dumps(old), encoding="utf-8")
    got = env.client.get(URL).json()
    assert got["master_gain_db"] == 0.0
    assert got["banks"][1][2]["stable_id"] == GRID
    body = {k: v for k, v in got.items() if k != "master_gain_db"}
    body["banks"] = [
        [None if s is None else {k: v for k, v in s.items() if k != "missing"} for s in b] for b in body["banks"]
    ]
    assert env.client.put(URL, json=body).status_code == 200
    assert json.loads(_file_path(env).read_text(encoding="utf-8"))["master_gain_db"] == 0.0


@pytest.mark.parametrize("gain", [-60.1, 0.5, 6.0])
def test_put_refuses_master_gain_out_of_range_and_stores_nothing(env: SimpleNamespace, gain: float) -> None:
    """[if] master_gain_db is outside -60..0 dB [then] 422 naming it and the stored document is unchanged, [else stop]."""
    assert env.client.put(URL, json={**_doc(b1s4=_track()), "master_gain_db": -6.0}).status_code == 200
    before = _file_path(env).read_bytes()
    r = env.client.put(URL, json={**_doc(), "master_gain_db": gain})
    assert r.status_code == 422, r.text
    assert "master_gain_db" in json.dumps(r.json()["detail"])
    assert _file_path(env).read_bytes() == before


def test_store_refuses_master_gain_out_of_range(tmp_path: Path) -> None:
    """[if] the store is handed master_gain_db -61, +1 or a bool directly [then] refused and no file, [else stop]."""
    state = tmp_path / "state.db"
    for gain in (-61.0, 1.0, True):
        with pytest.raises(sampler_banks.InvalidBanksDocument, match="master_gain_db"):
            sampler_banks.write_banks(state, {**_doc(), "master_gain_db": gain})
    assert not sampler_banks.banks_path(state).exists()
    assert sampler_banks.write_banks(state, {**_doc(), "master_gain_db": -60})["master_gain_db"] == -60.0
