"""Edge sampler sidecar store: ``apps/shared/state/sampler_store.py`` (``sampler.db``).

[if] the sampler store loses, reorders or misplaces a capture, meta, set, pin or flag [then] these fail, [else stop].

Regression one-liners:
  - if an upserted capture does not load back under its stable_id with its vector then broken
  - if deleting a capture leaves its slot meta or pin behind then broken
  - if a second meta write does not replace the first then broken
  - if a set's name, description or slot order does not round-trip then broken
  - if a set saved with slot_ids None does not read back None then broken
  - if re-pinning moves a slot, or unpinning keeps it, then broken
  - if an unset flag reads as anything but None, or a rewrite does not replace it, then broken
  - if the store writes state.db, or sampler.db is not beside it, then broken
"""

from __future__ import annotations

from pathlib import Path

from apps.shared.state import sampler_store

SID = "a" * 40
VECTOR = {"trim_db": -6.0, "eq_db": {"low": 0.0, "mid": -3.0, "high": 0.0}, "filter": 0.25}


def _capture(state: Path, in_ms: int = 4000, out_ms: int = 12000) -> None:
    sampler_store.upsert_capture(
        state, SID, in_ms=in_ms, out_ms=out_ms, beats=16, vector=VECTOR, created_at="2026-11-13T00:00:00Z"
    )


def _meta(state: Path, slot_id: str, name: str | None) -> None:
    sampler_store.upsert_meta(
        state, slot_id, SID, name=name, description="note", settings={"mode": "loop"}, updated_at="t1"
    )


def test_if_a_capture_is_upserted_then_it_loads_under_its_track(state_db_path: Path) -> None:
    """[if] a capture is upserted [then] load_all returns it under its stable_id with its vector, [else stop]."""
    _capture(state_db_path)
    captures = sampler_store.load_all(state_db_path)[SID].captures
    assert captures == [
        {"in_ms": 4000, "out_ms": 12000, "beats": 16, "vector": VECTOR, "created_at": "2026-11-13T00:00:00Z"}
    ]


def test_if_a_capture_is_deleted_then_its_meta_and_pin_go_too(state_db_path: Path) -> None:
    """[if] a capture is deleted [then] its capture, meta and pin rows are all gone, [else stop]."""
    slot_id = f"{SID}:capture:4000-12000"
    _capture(state_db_path)
    _meta(state_db_path, slot_id, "Vox")
    sampler_store.write_pin(state_db_path, slot_id, pinned=True, pinned_at="t1")
    sampler_store.delete_capture(state_db_path, SID, in_ms=4000, out_ms=12000, slot_id=slot_id)
    track = sampler_store.load_all(state_db_path).get(SID)
    assert (track, sampler_store.read_pins(state_db_path)) == (None, [])


def test_if_meta_is_written_twice_then_the_last_write_wins(state_db_path: Path) -> None:
    """[if] a slot's meta is upserted twice [then] load_all returns the second name and settings, [else stop]."""
    slot_id = f"{SID}:loop_hot_cue:4000-12000"
    _meta(state_db_path, slot_id, "First")
    sampler_store.upsert_meta(state_db_path, slot_id, SID, name=None, description="", settings=None, updated_at="t2")
    meta = sampler_store.load_all(state_db_path)[SID].meta[slot_id]
    assert meta == {"name": None, "description": "", "settings": None, "updated_at": "t2"}


def test_if_a_set_is_written_then_it_reads_back_in_order(state_db_path: Path) -> None:
    """[if] a set is written with a gap in its slot order [then] read_sets returns it unchanged, [else stop]."""
    stored = sampler_store.write_set(
        state_db_path, "right-1", name="Vox", description="warm-up", slot_ids=["s2", None, "s1"], updated_at="t1"
    )
    assert sampler_store.read_sets(state_db_path) == [stored]
    assert stored["slot_ids"] == ["s2", None, "s1"]


def test_if_a_set_has_no_slot_order_then_it_reads_back_none(state_db_path: Path) -> None:
    """[if] a set is written with slot_ids None [then] it reads back None (the ranked default), [else stop]."""
    sampler_store.write_set(state_db_path, "left-1", name="Drums", description="", slot_ids=None, updated_at="t1")
    assert sampler_store.read_sets(state_db_path)[0]["slot_ids"] is None


def test_if_a_slot_is_repinned_then_it_keeps_its_first_place(state_db_path: Path) -> None:
    """[if] a slot is pinned, another pinned, then the first re-pinned [then] the order holds, [else stop]."""
    sampler_store.write_pin(state_db_path, "s1", pinned=True, pinned_at="t1")
    sampler_store.write_pin(state_db_path, "s2", pinned=True, pinned_at="t2")
    assert sampler_store.write_pin(state_db_path, "s1", pinned=True, pinned_at="t3") == ["s1", "s2"]


def test_if_a_slot_is_unpinned_then_it_leaves_the_pins(state_db_path: Path) -> None:
    """[if] a pinned slot is unpinned [then] write_pin returns the pins without it, [else stop]."""
    sampler_store.write_pin(state_db_path, "s1", pinned=True, pinned_at="t1")
    assert sampler_store.write_pin(state_db_path, "s1", pinned=False, pinned_at="t2") == []


def test_if_a_flag_is_unset_then_none_and_a_rewrite_replaces_it(state_db_path: Path) -> None:
    """[if] a flag is never set [then] it reads None, and a second write replaces the first, [else stop]."""
    assert sampler_store.read_flag(state_db_path, "starter_deleted") is None
    sampler_store.write_flag(state_db_path, "starter_deleted", "0")
    sampler_store.write_flag(state_db_path, "starter_deleted", "1")
    assert sampler_store.read_flag(state_db_path, "starter_deleted") == "1"


def test_if_the_store_is_written_then_only_the_sidecar_exists(state_db_path: Path) -> None:
    """[if] the store is written [then] sampler.db sits beside state.db and state.db is untouched, [else stop]."""
    sampler_store.write_flag(state_db_path, "k", "v")
    sidecar = sampler_store.sidecar_path(state_db_path)
    assert (sidecar, sidecar.is_file(), state_db_path.exists()) == (state_db_path.parent / "sampler.db", True, False)
