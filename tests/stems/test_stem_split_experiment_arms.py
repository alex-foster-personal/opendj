"""H8: the split-experiment arm is recorded before the run and never relabelled.

`scripts/stem_split_runner.py` claims status `✔︎ ✅ 🎯` (done + working +
regression tests) for exactly these two behaviours in its own MINI-PRD. That
claim was false: nothing imported the module. Six months of accumulated split
data becomes uninterpretable the moment a re-run is allowed to rewrite an
existing assignment, and the loss is silent.

Acceptance criteria (UNCAPTURED-REQUIREMENTS.md H8):
  [if] the runner is invoked twice over the same library [then] every track
       keeps its original arm ⛔️ six months of split data becomes
       uninterpretable
  [if] the farm crashes mid-batch [then] the assignments for the whole intended
       batch are already in state.db ⛔️ the gap between intended and completed
       is invisible
  [if] the arm set changes [then] EXPERIMENT_ID changes and old rows are not
       pooled with new ⛔️ two different option spaces get averaged together

-Claude
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import schema
from scripts import stem_split_runner as ssr


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A real state.db carrying the real events schema."""
    state = tmp_path / "state"
    state.mkdir(parents=True)
    conn = sqlite3.connect(state / "state.db")
    try:
        schema.apply_migrations(conn)
        conn.commit()
    finally:
        conn.close()
    return tmp_path


def _rows(data_dir: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(data_dir / "state" / "state.db")
    try:
        return [
            (sid, json.loads(payload)["arm"])
            for sid, payload in conn.execute(
                "SELECT stable_id, payload_json FROM events WHERE kind = ? "
                "ORDER BY id",
                (ssr.EVENT_KIND,),
            )
        ]
    finally:
        conn.close()


# ----- first assignment wins ---------------------------------------------


@pytest.mark.requirement("STEM-SPLIT")
def test_a_second_wave_never_relabels_an_existing_assignment(data_dir: Path) -> None:
    ids = [f"sid-{i:03d}" for i in range(40)]
    written = ssr.record_assignments(
        data_dir, [(sid, ssr.assign_arm(sid)) for sid in ids], dry_run=False
    )
    assert written == len(ids)
    first = dict(_rows(data_dir))

    # A second wave that would assign EVERY track to a different arm. Nothing
    # may move, and nothing may be appended.
    other = next(a for a in ssr.ARMS if a.name != first[ids[0]])
    again = ssr.record_assignments(
        data_dir, [(sid, other) for sid in ids], dry_run=False
    )
    assert again == 0, f"{again} track(s) were relabelled by a re-run"
    assert dict(_rows(data_dir)) == first
    assert len(_rows(data_dir)) == len(ids), "a re-run appended duplicate rows"


@pytest.mark.requirement("STEM-SPLIT")
def test_a_second_wave_only_records_the_tracks_that_are_new(data_dir: Path) -> None:
    old = [f"sid-{i:03d}" for i in range(10)]
    ssr.record_assignments(
        data_dir, [(s, ssr.assign_arm(s)) for s in old], dry_run=False
    )
    mixed = old + [f"sid-new-{i}" for i in range(4)]
    written = ssr.record_assignments(
        data_dir, [(s, ssr.assign_arm(s)) for s in mixed], dry_run=False
    )
    assert written == 4
    assert len(_rows(data_dir)) == 14


@pytest.mark.requirement("STEM-SPLIT")
def test_dry_run_records_nothing(data_dir: Path) -> None:
    ids = [f"sid-{i}" for i in range(5)]
    assert ssr.record_assignments(
        data_dir, [(s, ssr.assign_arm(s)) for s in ids], dry_run=True
    ) == 5
    assert _rows(data_dir) == []


@pytest.mark.requirement("STEM-SPLIT")
def test_already_assigned_reads_back_what_was_recorded(data_dir: Path) -> None:
    ids = [f"sid-{i}" for i in range(6)]
    ssr.record_assignments(
        data_dir, [(s, ssr.assign_arm(s)) for s in ids], dry_run=False
    )
    assert ssr.already_assigned(data_dir) == set(ids)


# ----- assignment is deterministic and id-scoped -------------------------


@pytest.mark.requirement("STEM-SPLIT")
def test_assignment_is_stable_for_the_same_id() -> None:
    ids = [f"sid-{i:04d}" for i in range(200)]
    once = [ssr.assign_arm(s).name for s in ids]
    assert once == [ssr.assign_arm(s).name for s in ids]


@pytest.mark.requirement("STEM-SPLIT")
def test_assignment_is_derived_from_experiment_id_and_stable_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hash is EXPERIMENT_ID:stable_id, so a bumped id re-randomises.

    Salting only on the stable_id would keep every track on the same arm
    forever across experiments, which is not a split at all.
    """
    ids = [f"sid-{i:04d}" for i in range(300)]
    before = [ssr.assign_arm(s).name for s in ids]
    monkeypatch.setattr(ssr, "EXPERIMENT_ID", "stem-split-vX-test")
    after = [ssr.assign_arm(s).name for s in ids]
    assert before != after, (
        "bumping EXPERIMENT_ID did not change any assignment; the arm is not "
        "salted by the experiment"
    )


@pytest.mark.requirement("STEM-SPLIT")
def test_recorded_payload_names_the_experiment_and_its_arm_set(
    data_dir: Path,
) -> None:
    """Old rows must stay interpretable without reading today's source."""
    ssr.record_assignments(data_dir, [("sid-x", ssr.assign_arm("sid-x"))], dry_run=False)
    conn = sqlite3.connect(data_dir / "state" / "state.db")
    try:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM events WHERE kind = ?", (ssr.EVENT_KIND,)
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert payload["experiment"] == ssr.EXPERIMENT_ID
    assert payload["arms"] == [a.name for a in ssr.ARMS]
    assert payload["preset"] and payload["codec"]


@pytest.mark.requirement("STEM-SPLIT")
def test_experiment_id_must_move_when_the_arm_set_moves() -> None:
    """Pin the arm set to the id that labels it.

    Editing ARMS without bumping EXPERIMENT_ID silently pools two different
    option spaces under one label, and no amount of later analysis can
    un-average them. Changing an arm is legitimate; changing it WITHOUT moving
    the id is not, so this test fails on purpose and both values are updated
    together.
    """
    fingerprint = hashlib.sha256(
        "|".join(
            f"{a.name}:{a.preset}:{a.codec}:{a.weight}" for a in ssr.ARMS
        ).encode()
    ).hexdigest()[:16]
    assert (ssr.EXPERIMENT_ID, fingerprint) == (
        "stem-split-v2",
        "a444247cc6797019",
    ), (
        "the arm set changed. Bump EXPERIMENT_ID so v2 rows are not pooled "
        "with a different option space, then update this pin."
    )


# ----- recorded BEFORE the farm runs -------------------------------------


@pytest.mark.requirement("STEM-SPLIT")
def test_assignments_are_durable_before_the_first_arm_is_farmed(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash in the first arm must still leave the WHOLE batch assigned.

    The gap between intended and completed is only visible because the
    intention was written first.
    """
    ids = [f"sid-{i:03d}" for i in range(40)]
    monkeypatch.setattr(ssr, "_assert_arms_runnable", lambda: None)
    monkeypatch.setattr(ssr, "candidate_tracks", lambda d, limit: ids)  # noqa: ARG005

    def _crash(arm, arm_ids, dest, dry_run):  # noqa: ANN001, ARG001
        raise RuntimeError("modal died mid-batch")

    monkeypatch.setattr(ssr, "run_arm", _crash)

    with pytest.raises(RuntimeError, match="modal died"):
        ssr.main(["--data-dir", str(data_dir), "--limit", "0", "--dest", "none"])

    recorded = dict(_rows(data_dir))
    assert set(recorded) == set(ids), (
        f"only {len(recorded)}/{len(ids)} assignments survived the crash; the "
        "intended batch is not recoverable"
    )
    assert recorded == {s: ssr.assign_arm(s).name for s in ids}


@pytest.mark.requirement("STEM-SPLIT")
def test_plan_only_records_nothing_and_runs_nothing(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [f"sid-{i}" for i in range(8)]
    monkeypatch.setattr(ssr, "_assert_arms_runnable", lambda: None)
    monkeypatch.setattr(ssr, "candidate_tracks", lambda d, limit: ids)  # noqa: ARG005

    def _never(*args: object, **kwargs: object) -> int:
        raise AssertionError("--plan-only farmed an arm")

    monkeypatch.setattr(ssr, "run_arm", _never)
    assert ssr.main(["--data-dir", str(data_dir), "--plan-only"]) == 0
    assert _rows(data_dir) == []
