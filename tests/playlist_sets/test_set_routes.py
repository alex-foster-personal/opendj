"""Set routes (SET-6586): components, routes through them, sets that name a route.

- [if] a slot holds alternatives [then] list_routes counts every route and names saved sets, [else stop].
- [if] a component is edited [then] sets play the edit while earlier runs keep theirs, [else stop].
- [if] a component a set still uses is deleted [then] refuse and name the sets, [else stop].
- [if] a v1 database migrates to v2 [then] its snapshot sets are untouched, [else stop].
- [if] a write fails partway [then] none of it lands, and a caller's transaction stays open, [else stop].
- [if] a route repeats a slot or breaks slot order [then] refuse it, [else stop].
- [if] a live pick leaves the set's own slots [then] refuse it, [else stop].
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.shared.play_orders.schema import apply_play_order_migrations
from apps.shared.playlist_sets import (
    ComponentInUseError,
    create_component,
    create_playlist_set,
    delete_component,
    list_components,
    list_routes,
    list_slots,
    load_playlist_set,
    load_run_entries,
    record_run,
    update_component,
    update_set_parts,
)
from apps.shared.playlist_sets import schema as ps_schema
from apps.shared.playlist_sets.schema import apply_playlist_set_migrations
from apps.shared.state import db as state_db

pytestmark = pytest.mark.requirement("SET-6586")

# Invented stable ids; none matches a real library row.
OPENER_A = ["zq-open-a1", "zq-open-a2"]
OPENER_B = ["zq-open-b1"]
MAIN = ["zq-main-1", "zq-main-2"]
SUNRISE = ["zq-sun-1", "zq-shared-close"]
BANGER = ["zq-bang-1"]
SINGALONG = ["zq-sing-1", "zq-shared-close"]


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = state_db.open_rw(tmp_path / "state.db")
    apply_play_order_migrations(c)
    apply_playlist_set_migrations(c)
    yield c
    c.close()


def _party(conn: sqlite3.Connection) -> dict[str, int]:
    """Endings are created first on purpose: slot order, not insertion order, wins."""
    ids = {
        "sunrise": create_component(conn, "pl1", "Sunrise", "ending", SUNRISE, tags=["#sunrise"]),
        "banger": create_component(conn, "pl1", "Banger", "ending", BANGER),
        "singalong": create_component(conn, "pl1", "Singalong", "ending", SINGALONG),
        "warm": create_component(conn, "pl1", "Warm", "opener", OPENER_A, tags=["vocal"]),
        "straight": create_component(conn, "pl1", "Straight in", "opener", OPENER_B),
        "main": create_component(conn, "pl1", "Main room", "main", MAIN),
    }
    conn.commit()
    return ids


def test_slots_follow_the_ladder_not_insertion_order(conn: sqlite3.Connection) -> None:
    _party(conn)
    assert list_slots(conn, "pl1") == ["opener", "main", "ending"]
    names = [c.name for c in list_components(conn, "pl1")]
    assert names == ["Warm", "Straight in", "Main room", "Sunrise", "Banger", "Singalong"]


def test_custom_slot_lands_after_existing_slots(conn: sqlite3.Connection) -> None:
    _party(conn)
    create_component(conn, "pl1", "Afterparty", "afters", ["zq-after-1"])
    create_component(conn, "pl1", "Encore", "encore", ["zq-enc-1"])
    assert list_slots(conn, "pl1") == ["opener", "main", "ending", "encore", "afters"]


def test_tags_are_normalized(conn: sqlite3.Connection) -> None:
    ids = _party(conn)
    comps = {c.id: c for c in list_components(conn, "pl1")}
    assert comps[ids["sunrise"]].tags == ["sunrise"]
    assert comps[ids["warm"]].tags == ["vocal"]


def test_routes_count_every_combination_and_name_saved_sets(conn: sqlite3.Connection) -> None:
    ids = _party(conn)
    party = create_playlist_set(
        conn, "pl1", "AtlantisParty", parts=[ids["warm"], ids["main"], ids["banger"]]
    )
    listing = list_routes(conn, "pl1")
    assert listing.total == 6
    assert listing.truncated is False
    assert len(listing.routes) == 6
    by_parts = {tuple(r.parts): r for r in listing.routes}
    assert by_parts[(ids["warm"], ids["main"], ids["banger"])].set_id == party
    assert by_parts[(ids["warm"], ids["main"], ids["sunrise"])].set_id is None
    # Every route is one per slot, in slot order.
    for r in listing.routes:
        assert r.parts[0] in (ids["warm"], ids["straight"])
        assert r.parts[1] == ids["main"]


def test_route_listing_truncates_and_says_so(conn: sqlite3.Connection) -> None:
    _party(conn)
    listing = list_routes(conn, "pl1", limit=4)
    assert listing.total == 6
    assert len(listing.routes) == 4
    assert listing.truncated is True


def test_repeated_track_on_a_route_is_flagged(conn: sqlite3.Connection) -> None:
    ids = _party(conn)
    create_component(conn, "pl1", "Reprise", "encore", ["zq-shared-close"])
    routes = list_routes(conn, "pl1").routes
    flagged = [r for r in routes if r.repeats]
    assert flagged, "a route playing zq-shared-close twice must be flagged"
    assert all(r.repeats == ["zq-shared-close"] for r in flagged)
    assert all(ids["banger"] in r.parts for r in routes if not r.repeats)


def test_set_with_parts_resolves_live_and_runs_freeze(conn: sqlite3.Connection) -> None:
    ids = _party(conn)
    set_id = create_playlist_set(
        conn, "pl1", "Sunrise", parts=[ids["warm"], ids["main"], ids["sunrise"]]
    )
    ps = load_playlist_set(conn, set_id)
    assert ps.parts == [ids["warm"], ids["main"], ids["sunrise"]]
    assert [e.stable_id for e in ps.entries] == OPENER_A + MAIN + SUNRISE

    assert record_run(conn, set_id, "performance") == 1
    first_run = load_playlist_set(conn, set_id, include_runs=True).runs[-1].id

    update_component(conn, ids["sunrise"], stable_ids=["zq-sun-new"])
    edited = load_playlist_set(conn, set_id)
    assert [e.stable_id for e in edited.entries] == OPENER_A + MAIN + ["zq-sun-new"]
    # The earlier run keeps what it actually played.
    assert [sid for sid, _ in load_run_entries(conn, first_run)] == OPENER_A + MAIN + SUNRISE
    assert {cid for _, cid in load_run_entries(conn, first_run)} == {
        ids["warm"], ids["main"], ids["sunrise"]
    }


def test_live_ending_pick_is_recorded_on_the_run(conn: sqlite3.Connection) -> None:
    ids = _party(conn)
    set_id = create_playlist_set(
        conn, "pl1", "AtlantisParty", parts=[ids["warm"], ids["main"], ids["banger"]]
    )
    record_run(
        conn, set_id, "performance",
        played_parts=[ids["warm"], ids["main"], ids["singalong"]],
    )
    run_id = load_playlist_set(conn, set_id, include_runs=True).runs[-1].id
    assert [sid for sid, _ in load_run_entries(conn, run_id)] == OPENER_A + MAIN + SINGALONG
    # The set's own route is unchanged by a live pick.
    assert load_playlist_set(conn, set_id).parts == [ids["warm"], ids["main"], ids["banger"]]


def test_practice_freezes_entries_but_keeps_play_count(conn: sqlite3.Connection) -> None:
    ids = _party(conn)
    set_id = create_playlist_set(conn, "pl1", "S", parts=[ids["straight"], ids["main"]])
    assert record_run(conn, set_id, "practice") == 0
    run_id = load_playlist_set(conn, set_id, include_runs=True).runs[-1].id
    assert [sid for sid, _ in load_run_entries(conn, run_id)] == OPENER_B + MAIN


def test_v1_snapshot_set_runs_freeze_its_entries(conn: sqlite3.Connection) -> None:
    set_id = create_playlist_set(conn, "pl-flat", "Flat")
    assert load_playlist_set(conn, set_id).parts == []
    conn.executemany(
        "INSERT INTO playlist_set_entries(set_id, stable_id, position) VALUES (?, ?, ?)",
        [(set_id, "zq-flat-1", 0), (set_id, "zq-flat-2", 1)],
    )
    conn.commit()
    record_run(conn, set_id, "performance")
    run_id = load_playlist_set(conn, set_id, include_runs=True).runs[-1].id
    assert load_run_entries(conn, run_id) == [("zq-flat-1", None), ("zq-flat-2", None)]


def test_update_set_parts_replaces_route(conn: sqlite3.Connection) -> None:
    ids = _party(conn)
    set_id = create_playlist_set(conn, "pl1", "R", parts=[ids["warm"], ids["main"]])
    updated = update_set_parts(conn, set_id, [ids["straight"], ids["main"], ids["banger"]])
    assert updated.parts == [ids["straight"], ids["main"], ids["banger"]]


def test_delete_refuses_when_a_set_uses_the_component(conn: sqlite3.Connection) -> None:
    ids = _party(conn)
    set_id = create_playlist_set(conn, "pl1", "R", parts=[ids["warm"], ids["main"]])
    with pytest.raises(ComponentInUseError) as err:
        delete_component(conn, ids["main"])
    assert err.value.set_ids == [set_id]
    assert ids["main"] in {c.id for c in list_components(conn, "pl1")}
    # Control: an unused component does delete.
    delete_component(conn, ids["banger"])
    assert ids["banger"] not in {c.id for c in list_components(conn, "pl1")}


def test_update_validates_before_writing(conn: sqlite3.Connection) -> None:
    ids = _party(conn)
    with pytest.raises(ValueError, match="tag"):
        update_component(conn, ids["warm"], name="Renamed", tags=["  "])
    warm = next(c for c in list_components(conn, "pl1") if c.id == ids["warm"])
    assert (warm.name, warm.tags) == ("Warm", ["vocal"])


def test_failed_update_rolls_back_its_new_slot(conn: sqlite3.Connection) -> None:
    """A rename onto a taken name fails in SQL, after the new slot was written."""
    ids = _party(conn)
    with pytest.raises(sqlite3.IntegrityError):
        update_component(conn, ids["warm"], name="Main room", slot="afterparty")
    assert list_slots(conn, "pl1") == ["opener", "main", "ending"]
    warm = next(c for c in list_components(conn, "pl1") if c.id == ids["warm"])
    assert (warm.name, warm.slot) == ("Warm", "opener")
    assert not conn.in_transaction


def test_failed_create_rolls_back_its_new_slot(conn: sqlite3.Connection) -> None:
    _party(conn)
    with pytest.raises(sqlite3.IntegrityError):
        create_component(conn, "pl1", "Main room", "afterglow", ["zq-x"])
    assert list_slots(conn, "pl1") == ["opener", "main", "ending"]
    assert not conn.in_transaction


def test_write_inside_a_caller_transaction_does_not_commit_it(
    conn: sqlite3.Connection,
) -> None:
    """Control for the nested path: the caller decides, so its rollback wins."""
    _party(conn)
    conn.execute("BEGIN")
    create_component(conn, "pl1", "Late", "encore", ["zq-late-1"])
    assert conn.in_transaction
    conn.execute("ROLLBACK")
    assert "Late" not in {c.name for c in list_components(conn, "pl1")}
    assert list_slots(conn, "pl1") == ["opener", "main", "ending"]


def test_set_may_borrow_a_component_from_another_playlist(conn: sqlite3.Connection) -> None:
    ids = _party(conn)
    closer = create_component(conn, "pl-other", "Signature closer", "ending", ["zq-sig-1"])
    set_id = create_playlist_set(conn, "pl1", "Borrowed", parts=[ids["warm"], closer])
    assert [e.stable_id for e in load_playlist_set(conn, set_id).entries] == [*OPENER_A, "zq-sig-1"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"stable_ids": []}, "at least one track"),
        ({"stable_ids": ["  "]}, "stable_id"),
        ({"name": " "}, "component name"),
        ({"slot": ""}, "slot"),
    ],
)
def test_component_validation(conn: sqlite3.Connection, kwargs: dict, message: str) -> None:
    args = {"name": "X", "slot": "main", "stable_ids": ["zq-1"]} | kwargs
    with pytest.raises(ValueError, match=message):
        create_component(conn, "pl1", args["name"], args["slot"], args["stable_ids"])


def test_route_validation(conn: sqlite3.Connection) -> None:
    ids = _party(conn)
    with pytest.raises(ValueError, match="twice"):
        create_playlist_set(conn, "pl1", "D", parts=[ids["main"], ids["main"]])
    with pytest.raises(ValueError, match="at least one component"):
        create_playlist_set(conn, "pl1", "E", parts=[])
    with pytest.raises(LookupError):
        create_playlist_set(conn, "pl1", "F", parts=[999999])
    with pytest.raises(ValueError, match="not both"):
        create_playlist_set(conn, "pl1", "G", parts=[ids["main"]], from_play_order="x")


def test_route_takes_one_component_per_slot_in_slot_order(
    conn: sqlite3.Connection,
) -> None:
    ids = _party(conn)
    with pytest.raises(ValueError, match="one component per slot"):
        create_playlist_set(conn, "pl1", "Two endings", parts=[ids["sunrise"], ids["banger"]])
    with pytest.raises(ValueError, match="slot order"):
        create_playlist_set(conn, "pl1", "Backwards", parts=[ids["main"], ids["warm"]])
    set_id = create_playlist_set(conn, "pl1", "R", parts=[ids["warm"], ids["main"]])
    with pytest.raises(ValueError, match="one component per slot"):
        update_set_parts(conn, set_id, [ids["warm"], ids["straight"]])
    with pytest.raises(ValueError, match="one component per slot"):
        record_run(conn, set_id, "performance", played_parts=[ids["sunrise"], ids["banger"]])
    # Control: skipping a slot is a valid route, and nothing above was saved.
    skip = create_playlist_set(conn, "pl1", "No main", parts=[ids["warm"], ids["banger"]])
    assert load_playlist_set(conn, skip).parts == [ids["warm"], ids["banger"]]
    assert load_playlist_set(conn, set_id).parts == [ids["warm"], ids["main"]]


def test_borrowed_component_in_an_unknown_custom_slot_is_refused(
    conn: sqlite3.Connection,
) -> None:
    ids = _party(conn)
    odd = create_component(conn, "pl-other", "Odd", "interlude", ["zq-odd-1"])
    with pytest.raises(ValueError, match="'interlude' is not in playlist 'pl1'"):
        create_playlist_set(conn, "pl1", "Odd", parts=[ids["warm"], odd])


def test_slot_edit_that_breaks_a_saved_route_is_refused(conn: sqlite3.Connection) -> None:
    ids = _party(conn)
    set_id = create_playlist_set(conn, "pl1", "R", parts=[ids["warm"], ids["main"], ids["banger"]])
    with pytest.raises(ValueError, match=rf"sets \[{set_id}\]"):
        update_component(conn, ids["banger"], slot="opener")
    banger = next(c for c in list_components(conn, "pl1") if c.id == ids["banger"])
    assert banger.slot == "ending"
    # Control: the same edit on a component no set uses goes through.
    update_component(conn, ids["singalong"], slot="encore")
    assert list_slots(conn, "pl1") == ["opener", "main", "ending", "encore"]


def test_route_replacing_a_snapshot_keeps_old_runs_and_drops_dead_entries(
    conn: sqlite3.Connection,
) -> None:
    ids = _party(conn)
    set_id = create_playlist_set(conn, "pl-empty", "Was flat")
    # Shape of a set and run recorded before SET-6586: entries, no frozen run.
    conn.executemany(
        "INSERT INTO playlist_set_entries(set_id, stable_id, position) VALUES (?, ?, ?)",
        [(set_id, "zq-old-1", 0), (set_id, "zq-old-2", 1)],
    )
    record_run(conn, set_id, "performance")
    run_id = load_playlist_set(conn, set_id, include_runs=True).runs[-1].id
    conn.execute("DELETE FROM playlist_set_run_entries WHERE run_id=?", (run_id,))
    update_set_parts(conn, set_id, [ids["warm"], ids["main"]])
    assert load_run_entries(conn, run_id) == [("zq-old-1", None), ("zq-old-2", None)]
    left = conn.execute(
        "SELECT COUNT(*) FROM playlist_set_entries WHERE set_id=?", (set_id,)
    ).fetchone()[0]
    assert left == 0
    assert [e.stable_id for e in load_playlist_set(conn, set_id).entries] == [*OPENER_A, *MAIN]


def test_live_pick_must_fill_the_sets_own_slots(conn: sqlite3.Connection) -> None:
    ids = _party(conn)
    set_id = create_playlist_set(conn, "pl1", "R", parts=[ids["warm"], ids["main"], ids["sunrise"]])
    with pytest.raises(ValueError, match="must fill set"):
        record_run(conn, set_id, "performance", played_parts=[ids["warm"], ids["banger"]])
    with pytest.raises(ValueError, match="must fill set"):
        record_run(conn, set_id, "performance", played_parts=[ids["straight"], ids["main"]])
    encore = create_component(conn, "pl1", "Encore", "encore", ["zq-enc-1"])
    with pytest.raises(ValueError, match="must fill set"):
        record_run(conn, set_id, "performance", played_parts=[ids["warm"], ids["main"], encore])
    flat = create_playlist_set(conn, "pl-empty", "Flat")
    with pytest.raises(ValueError, match="no route"):
        record_run(conn, flat, "performance", played_parts=[ids["warm"], ids["main"]])
    assert load_playlist_set(conn, set_id).play_count == 0
    assert load_playlist_set(conn, flat).play_count == 0
    # Control: swapping both the opener and the ending within their slots is a live pick.
    assert record_run(
        conn, set_id, "performance",
        played_parts=[ids["straight"], ids["main"], ids["banger"]],
    ) == 1


def test_a_route_is_saved_once_per_playlist(conn: sqlite3.Connection) -> None:
    ids = _party(conn)
    first = create_playlist_set(conn, "pl1", "Gig", parts=[ids["warm"], ids["main"]])
    with pytest.raises(ValueError, match=f"set {first} on this playlist already"):
        create_playlist_set(conn, "pl1", "Gig again", parts=[ids["warm"], ids["main"]])
    other = create_playlist_set(conn, "pl1", "Other", parts=[ids["straight"], ids["main"]])
    with pytest.raises(ValueError, match=f"set {first} on this playlist already"):
        update_set_parts(conn, other, [ids["warm"], ids["main"]])
    # Control: re-saving a set's own route is not a clash.
    assert update_set_parts(conn, first, [ids["warm"], ids["main"]]).parts == [ids["warm"], ids["main"]]


def test_routes_list_saved_sets_that_skip_a_slot_or_borrow(
    conn: sqlite3.Connection,
) -> None:
    ids = _party(conn)
    full = list_routes(conn, "pl1")
    skip = create_playlist_set(conn, "pl1", "No main", parts=[ids["warm"], ids["sunrise"]])
    closer = create_component(conn, "pl-other", "Sig", "ending", ["zq-sig-1"])
    borrow = create_playlist_set(conn, "pl1", "Borrowed", parts=[ids["warm"], ids["main"], closer])
    listing = list_routes(conn, "pl1")
    assert listing.total == full.total + 2
    assert listing.routes[: full.total] == full.routes
    assert [(r.parts, r.set_id) for r in listing.routes[full.total:]] == [
        ([ids["warm"], ids["sunrise"]], skip),
        ([ids["warm"], ids["main"], closer], borrow),
    ]
    assert listing.routes[-1].stable_ids == [*OPENER_A, *MAIN, "zq-sig-1"]
    # Control: a saved full route is listed once, in place, not again as extra.
    gig = create_playlist_set(conn, "pl1", "Gig", parts=[ids["warm"], ids["main"], ids["banger"]])
    again = list_routes(conn, "pl1")
    assert again.total == listing.total
    assert [r.set_id for r in again.routes].count(gig) == 1
    cut = list_routes(conn, "pl1", limit=full.total + 1)
    assert (len(cut.routes), cut.truncated) == (full.total + 1, True)


def test_routes_of_a_playlist_with_only_borrowed_parts(conn: sqlite3.Connection) -> None:
    assert list_routes(conn, "pl-nothing").routes == []
    closer = create_component(conn, "pl-other", "Sig", "ending", ["zq-sig-1"])
    set_id = create_playlist_set(conn, "pl-borrow", "Only borrowed", parts=[closer])
    listing = list_routes(conn, "pl-borrow", limit=1)
    assert [(r.parts, r.set_id) for r in listing.routes] == [([closer], set_id)]
    assert (listing.total, listing.truncated) == (1, False)


def test_route_uniqueness_is_checked_inside_the_write_lock(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A second writer that checked before the first committed must still refuse."""
    ids = _party(conn)
    first = state_db.open_rw(tmp_path / "state.db")
    first.execute("BEGIN IMMEDIATE")
    held = create_playlist_set(first, "pl1", "First", parts=[ids["warm"], ids["main"]])
    outcome: list[BaseException | int] = []

    def _second() -> None:
        other = state_db.open_rw(tmp_path / "state.db")
        try:
            outcome.append(
                create_playlist_set(other, "pl1", "Second", parts=[ids["warm"], ids["main"]])
            )
        except BaseException as exc:  # noqa: BLE001 - the refusal is the result
            outcome.append(exc)
        finally:
            other.close()

    worker = threading.Thread(target=_second)
    worker.start()
    worker.join(timeout=0.5)
    assert worker.is_alive(), "the second writer must wait for the first one's lock"
    first.execute("COMMIT")
    first.close()
    worker.join(timeout=10)
    assert len(outcome) == 1
    assert isinstance(outcome[0], ValueError), outcome
    assert f"set {held}" in str(outcome[0])


def test_v1_database_migrates_without_touching_sets(tmp_path: Path) -> None:
    c = sqlite3.connect(tmp_path / "v1.db")
    ps_schema._ensure_meta(c)
    for stmt in ps_schema._V1:
        c.execute(stmt)
    c.execute(
        "INSERT INTO playlist_sets_schema_meta(version, applied_at) VALUES (1, 'x')"
    )
    c.execute(
        "INSERT INTO playlist_sets(id, playlist_id, name, play_count, created_at, updated_at) "
        "VALUES (7, 'pl1', 'Old', 3, 't', 't')"
    )
    c.executemany(
        "INSERT INTO playlist_set_entries(set_id, stable_id, position) VALUES (7, ?, ?)",
        [("zq-old", 0), ("zq-old-2", 1)],
    )
    c.executemany(
        "INSERT INTO playlist_set_runs(id, set_id, kind, created_at) VALUES (?, 7, ?, 't')",
        [(40, "performance"), (41, "practice")],
    )
    c.commit()
    assert apply_playlist_set_migrations(c) == 2
    old = load_playlist_set(c, 7)
    assert (old.name, old.play_count, old.parts) == ("Old", 3, [])
    assert [e.stable_id for e in old.entries] == ["zq-old", "zq-old-2"]
    # Runs recorded under v1 froze nothing; the migration gives each one the
    # snapshot it played, so its history is not reported empty.
    for run_id in (40, 41):
        assert load_run_entries(c, run_id) == [("zq-old", None), ("zq-old-2", None)]
    # Control: running the migrations again copies nothing twice.
    assert apply_playlist_set_migrations(c) == 2
    assert c.execute("SELECT COUNT(*) FROM playlist_set_run_entries").fetchone()[0] == 4
    c.close()


def _run_cli(db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "apps.playlist_sets", "--db", str(db), "--json", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_cli_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    c = state_db.open_rw(db)
    apply_play_order_migrations(c)
    apply_playlist_set_migrations(c)
    c.close()

    def make(name: str, slot: str, tracks: str) -> int:
        out = _run_cli(db, "component", "create", "--playlist", "pl1", "--name", name,
                       "--slot", slot, "--tracks", tracks, "--tag", "test")
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)["id"]

    opener = make("Warm", "opener", "zq-a,zq-b")
    end_a = make("Sunrise", "ending", "zq-c")
    end_b = make("Banger", "ending", "zq-d")

    routes = _run_cli(db, "routes", "--playlist", "pl1")
    assert routes.returncode == 0, routes.stderr
    body = json.loads(routes.stdout)
    assert body["total"] == 2
    assert [r["parts"] for r in body["routes"]] == [[opener, end_a], [opener, end_b]]

    created = _run_cli(db, "create", "--playlist", "pl1", "--name", "Gig",
                       "--parts", f"{opener},{end_a}")
    assert created.returncode == 0, created.stderr
    gig = json.loads(created.stdout)
    assert gig["parts"] == [opener, end_a]
    assert [e["stable_id"] for e in gig["entries"]] == ["zq-a", "zq-b", "zq-c"]

    performed = _run_cli(db, "perform", "--set", str(gig["id"]),
                         "--played-parts", f"{opener},{end_b}")
    assert performed.returncode == 0, performed.stderr
    assert json.loads(performed.stdout)["play_count"] == 1

    refused = _run_cli(db, "component", "delete", "--id", str(opener))
    assert refused.returncode == 1
    assert f"sets [{gig['id']}]" in refused.stderr

    both = _run_cli(db, "component", "update", "--id", str(end_b), "--tag", "x", "--clear-tags")
    assert both.returncode == 2
    cleared = _run_cli(db, "component", "update", "--id", str(end_b), "--clear-tags")
    assert cleared.returncode == 0, cleared.stderr
    assert json.loads(cleared.stdout)["tags"] == []

    listed = _run_cli(db, "component", "list", "--playlist", "pl1")
    assert [c["name"] for c in json.loads(listed.stdout)] == ["Warm", "Sunrise", "Banger"]


def test_cli_text_routes_flag_repeats_and_lists_reject_empty_items(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    c = state_db.open_rw(db)
    apply_play_order_migrations(c)
    apply_playlist_set_migrations(c)
    c.close()

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "apps.playlist_sets", "--db", str(db), *args],
            check=False, capture_output=True, text=True,
        )

    assert cli("component", "create", "--playlist", "pl1", "--name", "Warm",
               "--slot", "opener", "--tracks", "zq-a,zq-shared").returncode == 0
    assert cli("component", "create", "--playlist", "pl1", "--name", "Close",
               "--slot", "ending", "--tracks", "zq-shared").returncode == 0
    assert cli("component", "create", "--playlist", "pl1", "--name", "Fresh",
               "--slot", "ending", "--tracks", "zq-z").returncode == 0
    rows = cli("routes", "--playlist", "pl1").stdout.splitlines()
    # One route plays zq-shared twice and says so in plain text; the other does not.
    assert [r for r in rows if "repeats" in r] == [rows[0]]
    assert rows[0].endswith("repeats zq-shared")
    assert "repeats" not in rows[1]

    for bad in ("zq-a,,zq-b", "zq-a,", ","):
        out = cli("component", "create", "--playlist", "pl1", "--name", "Bad",
                  "--slot", "main", "--tracks", bad)
        assert out.returncode == 2, bad
        assert "empty item" in out.stderr
    out = cli("create", "--playlist", "pl1", "--name", "Gig", "--parts", "1,,3")
    assert out.returncode == 2
    assert "empty item" in out.stderr


def test_component_events_have_their_own_kind_and_set_events_keep_set_id(
    conn: sqlite3.Connection,
) -> None:
    ids = _party(conn)
    set_id = create_playlist_set(conn, "pl1", "Ev", parts=[ids["warm"], ids["main"]])
    update_component(conn, ids["banger"], tags=["loud"])
    rows = [
        (kind, json.loads(payload))
        for kind, payload in conn.execute("SELECT kind, payload_json FROM events ORDER BY id")
    ]
    sets = [p for k, p in rows if k == "playlist_set_changed"]
    comps = [p for k, p in rows if k == "playlist_component_changed"]
    # Control: both kinds were actually emitted, so the checks below bite.
    assert sets and comps
    assert all("set_id" in p for p in sets)
    assert {p["set_id"] for p in sets} == {set_id}
    assert all(not p["action"].startswith("component_") for p in sets)
    assert all("component_id" in p and "set_id" not in p for p in comps)
    assert {"component_created", "component_updated"} <= {p["action"] for p in comps}
