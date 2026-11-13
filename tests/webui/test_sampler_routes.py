"""Edge sampler routes: GET, PATCH, POST and DELETE /slots, /sets and /pins.

[if] a sampler route lists, ranks, pages, finds, edits, captures, sets or pins a slot wrongly [then] these fail, [else stop].

Regression one-liners:
  - if a listed loop cue's in_ms is not snapped to the nearest existing grid beat then broken
  - if a track with no grid yields a slot, or is not counted in skipped_no_grid, then broken
  - if peaks are not 120 points per band within 0..1 then broken
  - if compat ranks an out-of-window slot above an in-window one then broken
  - if offset/limit pages do not concatenate to the whole list, or slot ids change on reload, then broken
  - if an unknown slot id is not a 404 SLOT_NOT_FOUND then broken
  - if 2,000 slots list in 300 ms or more (warm) then broken
  - if a slot with no stored settings does not serve the defaults then broken
  - if a PATCH name over 60 chars or a level over 100 is accepted, or stores anything, then broken
  - if a renamed slot loses its name, description or settings after a restart, or hides its title, then broken
  - if a PATCH that omits a field changes it, or an empty name does not clear the name, then broken
  - if sort=recent does not put the last edited slot first then broken
  - if sort=crate splits a crate, breaks library crate order, or lists a crate-less slot early, then broken
  - if sort=color breaks hot cue palette order or lists a colourless slot early then broken
  - if a put set is not returned by GET /sets, or a set name over 40 chars is accepted, then broken
  - if a pin does not survive a restart, unpin does not clear it, or an unknown slot pins, then broken
  - if a posted capture is not listed on the beat with its vector, or writes an audio file, then broken
  - if a deleted capture still lists, keeps its pin, or a cue slot can be deleted, then broken
  - if a capture on an occupied place of a stored set loses or duplicates a slot then broken
  - if the same loop captured twice makes two slots then broken
  - if a capture or delete publishes no library.changed sampler event then broken
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared import events
from apps.webui.server import sampler_catalogue
from apps.webui.server.routes import sampler
from apps.webui.server.sampler_models import default_settings
from tests.webui.sampler_support import (
    BEAT_MS,
    GRID,
    NO_GRID,
    FakeSources,
    make_app,
    make_env,
    map_vendor,
    master,
    seed,
)


def _app(state: Path, sources: FakeSources) -> FastAPI:
    app = make_app(state, sources)
    app.include_router(sampler.router, prefix="/api/v1")
    return app


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    for base in make_env(tmp_path, monkeypatch):
        with TestClient(_app(base.state, base.sources)) as client:
            yield SimpleNamespace(**vars(base), client=client)


def _slots(client: TestClient, **params: Any) -> dict[str, Any]:
    r = client.get("/api/v1/sampler/slots", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _slot_a(env: SimpleNamespace) -> dict[str, Any]:
    return next(s for s in _slots(env.client)["slots"] if s["cue_letter"] == "A")


def test_if_a_loop_cue_starts_30_ms_late_then_the_listed_slot_is_on_the_beat(env: SimpleNamespace) -> None:
    """[if] a loop cue starts 30 ms after a beat [then] GET /slots lists it on that beat, [else stop]."""
    s = _slot_a(env)
    assert (s["slot_id"], s["in_ms"], s["out_ms"], s["beats"]) == (f"{GRID}:loop_hot_cue:4000-12000", 4000, 12000, 16)


def test_if_a_track_has_no_grid_then_the_page_counts_it_and_lists_no_slot(env: SimpleNamespace) -> None:
    """[if] a track has no usable grid [then] the page lists none of its slots and counts it, [else stop]."""
    page = _slots(env.client)
    assert (page["skipped_no_grid"], {s["stable_id"] for s in page["slots"]}, page["rekordbox"]) == (1, {GRID}, "ok")


def test_if_a_slot_is_listed_then_peaks_are_120_points_within_0_1(env: SimpleNamespace) -> None:
    """[if] a slot is listed [then] each band carries 120 peaks within 0..1, [else stop]."""
    peaks = _slot_a(env)["peaks"]
    assert all(len(peaks[b]) == 120 and all(0.0 <= v <= 1.0 for v in peaks[b]) for b in ("low", "mid", "high"))


def test_if_master_is_given_then_compat_ranks_in_window_bpm_first(env: SimpleNamespace) -> None:
    """[if] sort=compat with a 200 BPM master [then] the order is A, B (own row only, EDGESAMP-14822), [else stop]."""
    page = _slots(env.client, sort="compat", master_bpm=200.0, master_key="8A")
    assert [s["cue_letter"] for s in page["slots"]] == ["A", "B"]


def test_if_offset_and_limit_page_the_list_then_pages_join_to_the_whole_list(env: SimpleNamespace) -> None:
    """[if] the list is paged 1 at a time and reloaded [then] pages join to the same ids, [else stop]."""
    whole = [s["slot_id"] for s in _slots(env.client, sort="track")["slots"]]
    sampler_catalogue.reset_caches()
    paged = [
        s["slot_id"] for i in range(len(whole)) for s in _slots(env.client, sort="track", offset=i, limit=1)["slots"]
    ]
    assert (paged, len(whole), _slots(env.client, offset=len(whole))["slots"]) == (whole, 2, [])


def test_if_q_matches_nothing_then_no_slots(env: SimpleNamespace) -> None:
    """[if] q matches no name, title, artist or key [then] total is 0, [else stop]."""
    assert _slots(env.client, q="zzz-no-match")["total"] == 0


def test_if_sort_is_unknown_then_422(env: SimpleNamespace) -> None:
    """[if] sort names no known order [then] the list answers 422, [else stop]."""
    assert env.client.get("/api/v1/sampler/slots", params={"sort": "nope"}).status_code == 422


@contextmanager
def _library(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracks: dict[str, list[tuple[str, int, int]]],
    colors: dict[str, int] | None = None,
    rb_cues: list[tuple[int, int, int]] | None = None,
    rb_colors: dict[int, int] | None = None,
) -> Iterator[tuple[TestClient, FakeSources]]:
    sampler_catalogue.reset_caches()
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master(tmp_path / "master.db", rb_cues or [], rb_colors))
    state = tmp_path / "state" / "state.db"
    state.parent.mkdir()
    seed(state, tracks, colors)
    if rb_cues:
        map_vendor(state, next(iter(tracks)))
    sources = FakeSources(tmp_path / "unused.wav")
    with TestClient(_app(state, sources)) as client:
        yield client, sources
    sampler_catalogue.reset_caches()


def test_if_sort_is_crate_then_crates_are_contiguous_in_library_order_and_crateless_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] sort=crate [then] each crate's slots are contiguous, crates in library order, crate-less last, [else stop]."""
    t1, t2, t3, t4 = (str(d) * 40 for d in "1234")  # titles sort t1..t4, the opposite of the crates
    loops = [("A", 4000, 8000), ("B", 20000, 24000)]
    with _library(tmp_path, monkeypatch, {t: loops for t in (t1, t2, t3, t4)}) as (client, sources):
        sources.crate_members = [(t3,), (t2, t3), (t1,)]  # t3's first crate is crate 0; t4 is in none
        page = _slots(client, sort="crate")
    assert [s["stable_id"] for s in page["slots"]] == [t3, t3, t2, t2, t1, t1, t4, t4]


def test_if_sort_is_color_then_slots_follow_the_palette_order_and_colourless_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] sort=color [then] loops group by palette order, colour 0 and none last, [else stop].

    Own loops only: an own cue row hides the track's rekordbox loops (EDGESAMP-14822), so the
    uncoloured case is own cue E rather than a rekordbox memory loop.
    """
    own = [("A", 4030, 12000), ("B", 20000, 22010), ("C", 40010, 42000), ("D", 30000, 32000), ("E", 60000, 64000)]
    colors = {f"{GRID}:A": 42, f"{GRID}:B": 5, f"{GRID}:C": 22, f"{GRID}:D": 0}
    with _library(tmp_path, monkeypatch, {GRID: own}, colors) as (client, _):
        slots = _slots(client, sort="color")["slots"]
    assert [(s["cue_letter"], s["color_index"]) for s in slots] == [
        ("B", 5), ("C", 22), ("A", 42), ("D", 0), ("E", None)
    ]  # fmt: skip


def test_if_sort_is_a_near_miss_of_crate_then_422_naming_crate_and_color(env: SimpleNamespace) -> None:
    """[if] sort=crates is sent [then] 422 whose detail lists crate and color, never a default order, [else stop]."""
    r = env.client.get("/api/v1/sampler/slots", params={"sort": "crates"})
    assert (r.status_code, "'crate'" in r.text, "'color'" in r.text) == (422, True, True)


def test_if_a_listed_slot_is_fetched_by_id_then_it_matches_the_list(env: SimpleNamespace) -> None:
    """[if] a listed slot is fetched by its id [then] the same row comes back, [else stop]."""
    listed = _slot_a(env)
    assert env.client.get(f"/api/v1/sampler/slots/{listed['slot_id']}").json() == listed


def test_if_an_unknown_slot_is_fetched_then_404_slot_not_found(env: SimpleNamespace) -> None:
    """[if] a slot id is not in the catalogue [then] the answer is 404 SLOT_NOT_FOUND, [else stop]."""
    r = env.client.get(f"/api/v1/sampler/slots/{GRID}:capture:1-2")
    assert (r.status_code, r.json()["detail"]["code"]) == (404, "SLOT_NOT_FOUND")


def test_if_2000_slots_are_listed_warm_then_it_takes_under_300_ms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] 2,000 slots over 250 tracks are listed twice [then] warm is under 300 ms, analysed once, [else stop]."""
    sampler_catalogue.reset_caches()
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master(tmp_path / "master.db", []))
    state = tmp_path / "state" / "state.db"
    state.parent.mkdir()
    loop = [(letter, int(k * 8 * BEAT_MS), int((k * 8 + 4) * BEAT_MS)) for k, letter in enumerate("ABCDEFGH")]
    seed(state, {f"{i:040x}": loop for i in range(1, 251)})
    sources = FakeSources(tmp_path / "unused.wav")
    with TestClient(_app(state, sources)) as client:
        cold = client.get("/api/v1/sampler/slots", params={"limit": 5000}).json()
        t0 = time.perf_counter()
        warm = client.get("/api/v1/sampler/slots", params={"limit": 5000, "master_bpm": 124, "master_key": "8A"}).json()
        warm_ms = (time.perf_counter() - t0) * 1000
    sampler_catalogue.reset_caches()
    print(f"\n[sampler] 2,000-slot warm list: {warm_ms:.0f} ms, analysis calls {len(sources.analysis_calls)}")
    assert (cold["total"], len(warm["slots"]), len(sources.analysis_calls), warm_ms < 300) == (2000, 2000, 250, True)


# ---------- part 2: slot edits, sets and pins --------------------------------------


def _patch(client: TestClient, slot_id: str, **body: Any) -> Any:
    return client.patch(f"/api/v1/sampler/slots/{slot_id}", json=body)


def test_if_a_slot_has_no_stored_settings_then_it_serves_the_defaults(env: SimpleNamespace) -> None:
    """[if] a slot was never edited [then] it lists the default settings, no name and no description, [else stop]."""
    s = _slot_a(env)
    assert (s["settings"], s["name"], s["description"]) == (default_settings(is_loop=True), None, "")


def test_if_patch_name_is_over_60_chars_then_422(env: SimpleNamespace) -> None:
    """[if] PATCH carries a 61-char name [then] it answers 422, [else stop]."""
    assert _patch(env.client, _slot_a(env)["slot_id"], name="x" * 61).status_code == 422


def test_if_patch_level_is_out_of_range_then_422_and_nothing_is_stored(env: SimpleNamespace) -> None:
    """[if] PATCH carries level 101 [then] it answers 422 and the slot keeps its defaults, [else stop]."""
    slot_id = _slot_a(env)["slot_id"]
    r = _patch(env.client, slot_id, settings={**default_settings(is_loop=True), "level": 101})
    assert (r.status_code, _slot_a(env)["settings"]) == (422, default_settings(is_loop=True))


def test_if_a_slot_is_renamed_then_a_fresh_client_reads_the_name(env: SimpleNamespace) -> None:
    """[if] a slot is renamed with a description and mode [then] a restart reads them and the title, [else stop]."""
    slot = _slot_a(env)
    settings = {**default_settings(is_loop=True), "mode": "one_shot"}
    r = _patch(env.client, slot["slot_id"], name="Big stab", description="the drop", settings=settings)
    assert r.status_code == 200, r.text
    sampler_catalogue.reset_caches()
    with TestClient(_app(env.state, env.sources)) as fresh:
        got = fresh.get(f"/api/v1/sampler/slots/{slot['slot_id']}").json()
    assert (got["name"], got["description"], got["settings"], got["title"]) == (
        "Big stab",
        "the drop",
        settings,
        slot["title"],
    )
    assert r.json() == got


def test_if_patch_omits_a_field_then_it_keeps_and_an_empty_name_clears(env: SimpleNamespace) -> None:
    """[if] a PATCH sends only a description, then an empty name [then] each keeps the rest, [else stop]."""
    slot_id = _slot_a(env)["slot_id"]
    _patch(env.client, slot_id, name="Stab")
    kept = _patch(env.client, slot_id, description="note").json()
    cleared = _patch(env.client, slot_id, name="").json()
    assert ((kept["name"], kept["description"]), (cleared["name"], cleared["description"])) == (
        ("Stab", "note"),
        (None, "note"),
    )


def test_if_a_slot_is_edited_then_sort_recent_lists_it_first(env: SimpleNamespace) -> None:
    """[if] slot B is edited and nothing else is [then] sort=recent lists B first, [else stop]."""
    slot_b = next(s for s in _slots(env.client)["slots"] if s["cue_letter"] == "B")
    _patch(env.client, slot_b["slot_id"], description="fresh")
    assert _slots(env.client, sort="recent")["slots"][0]["slot_id"] == slot_b["slot_id"]


def test_if_a_set_is_put_then_get_sets_returns_it(env: SimpleNamespace) -> None:
    """[if] a named set with a gap is put [then] PUT echoes it and GET /sets returns it, [else stop]."""
    body = {"name": "Drops", "description": "big ones", "slot_ids": [_slot_a(env)["slot_id"], None]}
    r = env.client.put("/api/v1/sampler/sets/L1", json=body)
    assert (r.status_code, r.json()) == (200, {"id": "L1", **body})
    assert env.client.get("/api/v1/sampler/sets").json() == {"sets": [{"id": "L1", **body}]}


def test_if_a_set_name_is_over_40_chars_then_422(env: SimpleNamespace) -> None:
    """[if] PUT /sets carries a 41-char name [then] it answers 422 and no set is stored, [else stop]."""
    body = {"name": "x" * 41, "description": "", "slot_ids": None}
    r = env.client.put("/api/v1/sampler/sets/L1", json=body)
    assert (r.status_code, env.client.get("/api/v1/sampler/sets").json()) == (422, {"sets": []})


def test_if_a_slot_is_pinned_then_a_fresh_client_reads_the_pin_and_unpin_clears_it(env: SimpleNamespace) -> None:
    """[if] a slot is pinned and the app restarts [then] the pin is read back and unpin clears it, [else stop]."""
    slot_id = _slot_a(env)["slot_id"]
    pinned = env.client.put(f"/api/v1/sampler/pins/{slot_id}", json={"pinned": True})
    assert pinned.status_code == 200, pinned.text
    sampler_catalogue.reset_caches()
    with TestClient(_app(env.state, env.sources)) as fresh:
        after_restart = fresh.get("/api/v1/sampler/pins").json()
        unpinned = fresh.put(f"/api/v1/sampler/pins/{slot_id}", json={"pinned": False}).json()
    assert (pinned.json(), after_restart, unpinned) == ({"pins": [slot_id]}, {"pins": [slot_id]}, {"pins": []})


def test_if_an_unknown_slot_is_pinned_then_404_and_nothing_is_stored(env: SimpleNamespace) -> None:
    """[if] an id not in the catalogue is pinned [then] 404 SLOT_NOT_FOUND and no pin is stored, [else stop]."""
    r = env.client.put(f"/api/v1/sampler/pins/{GRID}:capture:0-1", json={"pinned": True})
    assert (r.status_code, r.json()["detail"]["code"], env.client.get("/api/v1/sampler/pins").json()) == (
        404,
        "SLOT_NOT_FOUND",
        {"pins": []},
    )


# ---------- part 3: capture and delete ---------------------------------------------

VECTOR = {
    "trim_db": -6.0,
    "eq_db": {"low": -3.0, "mid": 0.0, "high": 1.5},
    "filter": -0.25,
    "stems": None,
    "key_shift_st": 2,
    "master_tempo": True,
}


def _capture(client: TestClient, **over: Any) -> Any:
    body = {
        "stable_id": GRID,
        "in_ms": 30_240,
        "out_ms": 32_200,
        "beats": 4,
        "vector": VECTOR,
        "set_id": "L1",
        "index": 0,
    }
    return client.post("/api/v1/sampler/slots", json={**body, **over})


class _Hub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.events.append((topic, payload))


def test_if_a_capture_is_posted_off_the_beat_then_get_slots_lists_it_on_the_grid_with_its_vector(
    env: SimpleNamespace,
) -> None:
    """[if] a 4-beat capture is posted 240 ms late [then] GET /slots lists it on the beat with its vector, [else stop]."""
    created = _capture(env.client)
    assert created.status_code == 201, created.text
    listed = {s["slot_id"]: s for s in _slots(env.client)["slots"]}
    slot = listed[f"{GRID}:capture:30000-32000"]
    assert (slot["source"], slot["beats"], slot["vector"], created.json()) == ("capture", 4, VECTOR, slot)


def test_if_a_capture_is_posted_then_no_audio_file_is_written(env: SimpleNamespace) -> None:
    """[if] a capture is posted [then] its only new files are sqlite (sampler.db, state.db journal), no audio, [else stop]."""
    before = {p.name for p in env.tmp.rglob("*") if p.is_file()}
    assert _capture(env.client).status_code == 201
    new = {p.name for p in env.tmp.rglob("*") if p.is_file()} - before
    assert "sampler.db" in new and all(n.startswith(("sampler.db", "state.db")) for n in new), new


def test_if_a_capture_is_deleted_then_get_slots_no_longer_lists_it(env: SimpleNamespace) -> None:
    """[if] a posted capture is deleted [then] 204 and GET /slots and GET /slots/{id} no longer find it, [else stop]."""
    slot_id = _capture(env.client).json()["slot_id"]
    deleted = env.client.delete(f"/api/v1/sampler/slots/{slot_id}")
    remaining = {s["slot_id"] for s in _slots(env.client)["slots"]}
    gone = env.client.get(f"/api/v1/sampler/slots/{slot_id}").status_code
    assert (deleted.status_code, slot_id in remaining, gone) == (204, False, 404)


def test_if_a_pinned_capture_is_deleted_then_its_pin_goes_too(env: SimpleNamespace) -> None:
    """[if] a pinned capture is deleted [then] GET /pins no longer holds it, [else stop]."""
    slot_id = _capture(env.client).json()["slot_id"]
    env.client.put(f"/api/v1/sampler/pins/{slot_id}", json={"pinned": True})
    env.client.delete(f"/api/v1/sampler/slots/{slot_id}")
    assert env.client.get("/api/v1/sampler/pins").json() == {"pins": []}


def test_if_a_loop_cue_slot_is_deleted_then_409_and_it_still_lists(env: SimpleNamespace) -> None:
    """[if] a loop hot cue slot is deleted [then] 409 NOT_A_CAPTURE and it is still listed, [else stop]."""
    slot_id = _slot_a(env)["slot_id"]
    r = env.client.delete(f"/api/v1/sampler/slots/{slot_id}")
    assert (r.status_code, r.json()["detail"]["code"], _slot_a(env)["slot_id"]) == (409, "NOT_A_CAPTURE", slot_id)


def test_if_a_capture_targets_a_track_with_no_grid_then_422_and_nothing_is_listed(env: SimpleNamespace) -> None:
    """[if] a capture targets the no-grid track [then] 422 NO_BEATGRID and no capture lists, [else stop]."""
    r = _capture(env.client, stable_id=NO_GRID, in_ms=0, out_ms=2000)
    captured = [s for s in _slots(env.client)["slots"] if s["source"] == "capture"]
    assert (r.status_code, r.json()["detail"]["code"], captured) == (422, "NO_BEATGRID", [])


def test_if_a_capture_runs_past_the_grid_then_422_region_off_grid(env: SimpleNamespace) -> None:
    """[if] a capture's beats run past the last grid beat [then] 422 REGION_OFF_GRID, [else stop]."""
    r = _capture(env.client, in_ms=239_000, out_ms=240_000, beats=8)
    assert (r.status_code, r.json()["detail"]["code"]) == (422, "REGION_OFF_GRID")


def test_if_a_capture_targets_an_unknown_track_then_404_and_nothing_is_stored(env: SimpleNamespace) -> None:
    """[if] a capture names a track not in the library [then] 404 TRACK_NOT_FOUND and nothing lists, [else stop]."""
    r = _capture(env.client, stable_id="c" * 40)
    captured = [s for s in _slots(env.client)["slots"] if s["source"] == "capture"]
    assert (r.status_code, r.json()["detail"]["code"], captured) == (404, "TRACK_NOT_FOUND", [])


def test_if_a_capture_lands_on_an_occupied_place_then_it_is_inserted_and_nothing_is_lost(env: SimpleNamespace) -> None:
    """[if] a capture is dropped on place 1 of a stored 3-place set [then] it sits there, the rest move down, [else stop]."""
    layout = [_slot_a(env)["slot_id"], "x:loop_hot_cue:0-1", None]
    env.client.put("/api/v1/sampler/sets/L1", json={"name": "Drops", "description": "", "slot_ids": layout})
    slot_id = _capture(env.client, index=1).json()["slot_id"]
    stored = env.client.get("/api/v1/sampler/sets").json()["sets"][0]["slot_ids"]
    assert stored == [layout[0], slot_id, layout[1], None]


def test_if_a_capture_targets_a_ranked_set_then_the_layout_stays_ranked(env: SimpleNamespace) -> None:
    """[if] a capture targets a set with no stored layout [then] the set keeps slot_ids None, [else stop]."""
    env.client.put("/api/v1/sampler/sets/L1", json={"name": "All", "description": "", "slot_ids": None})
    assert _capture(env.client).status_code == 201
    assert env.client.get("/api/v1/sampler/sets").json()["sets"][0]["slot_ids"] is None


def test_if_the_same_loop_is_captured_twice_then_it_is_one_slot(env: SimpleNamespace) -> None:
    """[if] the same deck loop is captured twice (drag, then agent) [then] one slot, once in the set, [else stop]."""
    env.client.put("/api/v1/sampler/sets/L1", json={"name": "Drops", "description": "", "slot_ids": []})
    first, second = _capture(env.client).json(), _capture(env.client, in_ms=30_010).json()
    captured = [s["slot_id"] for s in _slots(env.client)["slots"] if s["source"] == "capture"]
    stored = env.client.get("/api/v1/sampler/sets").json()["sets"][0]["slot_ids"]
    assert (first, captured, stored) == (second, [first["slot_id"]], [first["slot_id"]])


def test_if_a_capture_is_posted_then_sort_recent_lists_it_first(env: SimpleNamespace) -> None:
    """[if] a capture is posted after slot B was edited [then] sort=recent lists the capture first, [else stop]."""
    slot_b = next(s for s in _slots(env.client)["slots"] if s["cue_letter"] == "B")
    _patch(env.client, slot_b["slot_id"], description="older")
    slot_id = _capture(env.client).json()["slot_id"]
    assert [s["slot_id"] for s in _slots(env.client, sort="recent")["slots"][:2]] == [slot_id, slot_b["slot_id"]]


def test_if_a_capture_is_posted_and_deleted_then_each_publishes_a_sampler_change(env: SimpleNamespace) -> None:
    """[if] a capture is posted then deleted [then] two library.changed sampler events name its track, [else stop]."""
    hub = _Hub()
    events.set_hub(hub)
    try:
        slot_id = _capture(env.client).json()["slot_id"]
        env.client.delete(f"/api/v1/sampler/slots/{slot_id}")
    finally:
        events.set_hub(None)
    assert hub.events == [("library.changed", {"kind": "sampler", "ids": [GRID]})] * 2
