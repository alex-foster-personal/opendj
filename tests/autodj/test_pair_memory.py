"""MEMORY-01..03: the saved transition per ordered pair, and save-on-run.

[if] a plan runs [then] it is that pair's default, [else stop].
[if] the same plan runs again [then] only its run count moves, [else stop].
[if] a different plan runs [then] it wins and its count starts again, [else stop].
[if] a document is not a plan [then] nothing is stored, [else stop].
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.autodj.pair_memory import (
    PairNotFound,
    clear_pair_transition,
    get_pair_transition,
    on_decks,
    record_played,
    save_pair_transition,
)
from apps.autodj.plan import PlanError, parse_plan
from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema

pytestmark = pytest.mark.requirement("MEMORY-02")


def plan_document(out_id: str = "out-track", in_id: str = "in-track", *, fade_bars: float = 8.0) -> dict[str, Any]:
    return {
        "out": {"deck": 1, "track": out_id, "window": {"start_bar": 65, "bars": 16}},
        "in": {"deck": 2, "track": in_id, "window": {"start_bar": 9, "bars": 16}},
        "style": "crossfade",
        "events": [
            {"at_bar": 0, "deck": 2, "do": "play"},
            {"at_bar": 0, "deck": None, "ramp": {"crossfader": 1.0}, "over_bars": fade_bars},
            {"at_bar": 16, "deck": 2, "do": "master"},
            {"at_bar": 16, "deck": 1, "do": "stop"},
        ],
    }


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = state_db.open_rw(tmp_path / "state.db")
    state_schema.apply_migrations(connection)
    yield connection
    connection.close()


def test_most_recent_wins_and_a_miss_is_none(conn: sqlite3.Connection) -> None:
    assert get_pair_transition(conn, "out-track", "in-track") is None
    save_pair_transition(conn, plan_document(), source="planner", style="crossfade")
    latest = save_pair_transition(conn, plan_document(fade_bars=4.0), source="edited", style="crossfade")
    assert latest.source == "edited"
    assert latest.plan["events"][1]["over_bars"] == 4.0
    assert get_pair_transition(conn, "out-track", "in-track") == latest
    assert conn.execute("SELECT COUNT(*) FROM pair_transitions").fetchone()[0] == 1


def test_a_pair_is_ordered(conn: sqlite3.Connection) -> None:
    save_pair_transition(conn, plan_document("a", "b"), source="planner", style="crossfade")
    assert get_pair_transition(conn, "b", "a") is None


def test_save_on_run_stores_the_plan_that_ran(conn: sqlite3.Connection) -> None:
    saved = record_played(conn, "out-track", "in-track", plan=plan_document(), source="planner", style="crossfade")
    assert (saved.source, saved.style, saved.times_played) == ("planner", "crossfade", 1)
    assert parse_plan(saved.plan).out.track == "out-track"


def test_a_rewound_retry_of_the_same_plan_only_counts(conn: sqlite3.Connection) -> None:
    record_played(conn, "out-track", "in-track", plan=plan_document(), source="captured", style="captured")
    again = record_played(conn, "out-track", "in-track", plan=plan_document(), source="planner", style="crossfade")
    assert (again.times_played, again.source, again.style) == (2, "captured", "captured")


def test_an_improved_plan_sticks_and_its_count_starts_again(conn: sqlite3.Connection) -> None:
    record_played(conn, "out-track", "in-track", plan=plan_document(), source="planner", style="crossfade")
    better = record_played(
        conn, "out-track", "in-track", plan=plan_document(fade_bars=4.0), source="captured", style="captured"
    )
    assert (better.times_played, better.source) == (1, "captured")


def test_played_without_a_plan_needs_a_saved_one(conn: sqlite3.Connection) -> None:
    with pytest.raises(PairNotFound):
        record_played(conn, "out-track", "in-track")
    save_pair_transition(conn, plan_document(), source="planner", style="crossfade")
    assert record_played(conn, "out-track", "in-track").times_played == 1


@pytest.mark.parametrize(
    ("document", "pair", "source", "message"),
    [
        ({"style": "x"}, ("out-track", "in-track"), "planner", "events"),
        (plan_document(), ("someone", "else"), "planner", "not this pair"),
        (plan_document(), ("out-track", "in-track"), "guessed", "source must be"),
    ],
)
def test_a_bad_save_stores_nothing(
    conn: sqlite3.Connection, document: dict[str, Any], pair: tuple[str, str], source: str, message: str
) -> None:
    with pytest.raises(PlanError, match=message):
        record_played(conn, *pair, plan=document, source=source, style="crossfade")
    assert conn.execute("SELECT COUNT(*) FROM pair_transitions").fetchone()[0] == 0


def test_clear_says_whether_there_was_one(conn: sqlite3.Connection) -> None:
    save_pair_transition(conn, plan_document(), source="planner", style="crossfade")
    assert clear_pair_transition(conn, "out-track", "in-track") is True
    assert clear_pair_transition(conn, "out-track", "in-track") is False


def test_on_decks_moves_every_event_and_mirrors_the_crossfader_across_it() -> None:
    moved = on_decks(plan_document(), out_deck=2, in_deck=1)
    assert (moved["out"]["deck"], moved["in"]["deck"]) == (2, 1)
    assert [event["deck"] for event in moved["events"]] == [1, None, 1, 2]
    assert moved["events"][1]["ramp"] == {"crossfader": 0.0}


def test_on_decks_on_the_same_side_leaves_the_crossfader() -> None:
    moved = on_decks(plan_document(), out_deck=3, in_deck=4)
    assert [event["deck"] for event in moved["events"]] == [4, None, 4, 3]
    assert moved["events"][1]["ramp"] == {"crossfader": 1.0}
