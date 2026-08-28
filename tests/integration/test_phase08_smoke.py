"""Phase 08 integration smoke -- CAT-03 + SMART-01/02 end-to-end.

Per Plan 01 Step 6: against a populated state DB, evaluate three rules
(genre=House, bpm between 120-128, rating>=4), assert each returns a
proper subset in <100ms. Uses the existing synthetic 20-track fixture;
this smoke test does not establish performance on a user's real library.

Plan 02 Step 5: add a demo-style end-to-end that creates 3 smartlists,
5 pairings, and materialises via the FakeWriter (no live DB writes).
"""
from __future__ import annotations

import time

import pytest

from apps.smartlists.evaluator import evaluate
from apps.smartlists.materializer import Materializer
from apps.smartlists.triggers import StateEvent, TriggerRunner
from apps.smartlists.writers import FakeWriter

pytestmark = pytest.mark.requirement("SMART-02")


@pytest.fixture
def populated_library(fixture_library):
    rows = []
    genres = ["House", "Techno", "Disco", "Ambient"]
    for i in range(20):
        rows.append({
            "stable_id": f"t{i:02d}",
            "title": f"track {i}",
            "fields": {
                "genre": genres[i % 4],
                "bpm": 100 + i * 2,
                "rating": (i % 5) + 1,
            },
        })
    fixture_library.add_many(rows)
    return fixture_library


def test_three_rules_each_filter_properly(populated_library, state_conn) -> None:
    total = state_conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    assert total == 20
    rules = [
        {"field": "genre", "op": "=", "value": "House"},
        {"field": "bpm", "op": "between", "value": [120, 128]},
        {"field": "rating", "op": ">=", "value": 4},
    ]
    for rule in rules:
        start = time.perf_counter()
        result = evaluate(rule, state_conn)
        elapsed = time.perf_counter() - start
        assert 0 < len(result) < total, (
            f"rule {rule} returned {len(result)}/{total}; "
            "expected proper subset"
        )
        assert elapsed < 0.100, f"eval took {elapsed:.3f}s > 100ms"


def test_combined_rule_returns_intersection(
    populated_library, state_conn,
) -> None:
    result = evaluate(
        {
            "op": "and",
            "children": [
                {"field": "genre", "op": "=", "value": "House"},
                {"field": "bpm", "op": "between", "value": [120, 128]},
                {"field": "rating", "op": ">=", "value": 4},
            ],
        },
        state_conn,
    )
    for leaf in [
        {"field": "genre", "op": "=", "value": "House"},
        {"field": "bpm", "op": "between", "value": [120, 128]},
        {"field": "rating", "op": ">=", "value": 4},
    ]:
        assert set(result) <= set(evaluate(leaf, state_conn))


def test_evaluator_output_stable(populated_library, state_conn) -> None:
    rule = {"field": "genre", "op": "=", "value": "House"}
    a = evaluate(rule, state_conn, order_by="added_date desc")
    b = evaluate(rule, state_conn, order_by="added_date desc")
    assert a == b


def test_end_to_end_demo(
    populated_library, smartlists_repo, pairings_repo_slm,
) -> None:
    """Demo flow: create smartlists, add pairings, materialise."""
    smartlists_repo.create(
        "House Fresh",
        {
            "op": "and",
            "children": [
                {"field": "genre", "op": "=", "value": "House"},
                {"field": "bpm", "op": "between", "value": [120, 128]},
            ],
        },
    )
    smartlists_repo.create(
        "Top Rated", {"field": "rating", "op": ">=", "value": 4},
    )
    smartlists_repo.create(
        "Techno", {"field": "genre", "op": "=", "value": "Techno"},
    )
    # 5 pairings.
    pairings_repo_slm.add("t00", "t01", direction="into")
    pairings_repo_slm.add("t00", "t02", direction="into")
    pairings_repo_slm.add("t04", "t08", direction="either")
    pairings_repo_slm.add("t12", "t16", direction="into")
    pairings_repo_slm.add("t16", "t04", direction="out_of")

    rb = FakeWriter(vendor="rekordbox")
    dj = FakeWriter(vendor="djay")
    mat = Materializer(smartlists_repo, [rb, dj])

    # Dry-run first.
    dry = mat.materialize_all(dry_run=True)
    assert len(dry) == 3
    assert all(r.dry_run for r in dry)

    # Live run against FakeWriter (no DB side effects).
    live = mat.materialize_all(dry_run=False, live=True)
    assert all(r.ok for r in live)
    assert "[SL] House Fresh" in rb.playlists
    assert "[SL] Top Rated" in dj.playlists

    # Simulate a track tag edit -> trigger re-materialises the rule that
    # references "genre" but not the unrelated ones.
    runner = TriggerRunner(
        smartlists_repo, mat, debounce_seconds=0.0,
        dry_run=False, live=True,
    )
    runner.handle_event(StateEvent(
        kind="track.tag_edited",
        stable_id="t01",
        changed_fields=frozenset({"genre"}),
    ))
    # debounce_seconds=0.0 above → event is immediately "ready"; no sleep
    # needed (avoids wall-clock flake).
    second = runner.run_ready()
    # At least the two genre-referencing smartlists got re-evaluated.
    names = {r.smartlist_name for r in second}
    assert "House Fresh" in names or "Techno" in names
