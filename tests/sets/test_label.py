"""Tests for :mod:`apps.sets.label`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.sets import label as label_mod


def _write_transitions(sets_root: Path, session_id: str, transitions: list[dict]) -> None:
    sess = sets_root / session_id
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "transitions.jsonl").write_text(
        "\n".join(json.dumps(t) for t in transitions) + "\n"
    )


@pytest.fixture
def transitions_fixture(sets_root: Path) -> list[dict]:
    """3 transitions with predicted classes."""
    rows = [
        {"idx": 0, "from_track": "t0a", "to_track": "t0b",
         "predicted_class": "cut", "confidence": 0.9,
         "features": {"overlap_s": 0.1, "fade_s": 0.0}},
        {"idx": 1, "from_track": "t1a", "to_track": "t1b",
         "predicted_class": "blend", "confidence": 0.75,
         "features": {"overlap_s": 10.0, "fade_s": 4.0}},
        {"idx": 2, "from_track": "t2a", "to_track": "t2b",
         "predicted_class": "quick_double", "confidence": 0.85,
         "features": {"overlap_s": 1.0, "fade_s": 0.2}},
    ]
    _write_transitions(sets_root, "s1", rows)
    return rows


@pytest.mark.requirement("SET-02")
def test_append_label_writes_jsonl_row(sets_root: Path):
    label_mod.append_label(
        "s1", 0, "cut", sets_root=sets_root, labeler="test-bot"
    )
    labels = label_mod.read_labels("s1", sets_root=sets_root)
    assert labels[0] == "cut"
    raw = (sets_root / "s1" / "labels.jsonl").read_text().strip()
    row = json.loads(raw)
    assert row["labeler"] == "test-bot"
    assert "labeled_at" in row


@pytest.mark.requirement("SET-02")
def test_append_label_rejects_unknown_class(sets_root: Path):
    with pytest.raises(ValueError):
        label_mod.append_label("s1", 0, "mystery", sets_root=sets_root)


@pytest.mark.requirement("SET-02")
def test_read_labels_returns_latest_per_idx(sets_root: Path):
    label_mod.append_label("s1", 0, "cut", sets_root=sets_root)
    label_mod.append_label("s1", 0, "blend", sets_root=sets_root)  # overwrite
    labels = label_mod.read_labels("s1", sets_root=sets_root)
    assert labels[0] == "blend"


@pytest.mark.requirement("SET-02")
def test_label_session_interactive_scripted(
    sets_root: Path,
    transitions_fixture: list[dict],
):
    answers = iter(["c", "b", "q"])  # cut, blend, quick_double
    label_mod.label_session(
        "s1",
        sets_root=sets_root,
        input_fn=lambda prompt: next(answers),
        print_fn=lambda msg: None,
    )
    labels = label_mod.read_labels("s1", sets_root=sets_root)
    assert labels == {0: "cut", 1: "blend", 2: "quick_double"}


@pytest.mark.requirement("SET-02")
def test_label_session_skip_and_accept_predicted(
    sets_root: Path,
    transitions_fixture: list[dict],
):
    # Skip idx=0, accept predicted for idx=1 (blend), skip idx=2.
    answers = iter(["s", "", "s"])
    label_mod.label_session(
        "s1",
        sets_root=sets_root,
        input_fn=lambda prompt: next(answers),
        print_fn=lambda msg: None,
    )
    labels = label_mod.read_labels("s1", sets_root=sets_root)
    assert labels == {1: "blend"}


@pytest.mark.requirement("SET-02")
def test_label_session_resumable_without_relabel(
    sets_root: Path,
    transitions_fixture: list[dict],
):
    # Label idx=0 first.
    label_mod.append_label("s1", 0, "cut", sets_root=sets_root)
    messages: list[str] = []
    answers = iter(["b", "q"])  # labels for idx=1 and 2
    label_mod.label_session(
        "s1",
        sets_root=sets_root,
        input_fn=lambda prompt: next(answers),
        print_fn=messages.append,
    )
    labels = label_mod.read_labels("s1", sets_root=sets_root)
    assert labels == {0: "cut", 1: "blend", 2: "quick_double"}


@pytest.mark.requirement("SET-02")
def test_label_session_with_no_transitions_prints_message(sets_root: Path):
    messages: list[str] = []
    label_mod.label_session(
        "empty",
        sets_root=sets_root,
        input_fn=lambda p: "",
        print_fn=messages.append,
        transitions=[],
    )
    assert any("no transitions" in m for m in messages)


@pytest.mark.requirement("SET-02")
def test_label_session_bad_input_reprompts(
    sets_root: Path,
    transitions_fixture: list[dict],
):
    messages: list[str] = []
    answers = iter(["z", "c", "s", "s"])  # bad key, then cut, then skip x2
    label_mod.label_session(
        "s1",
        sets_root=sets_root,
        input_fn=lambda p: next(answers),
        print_fn=messages.append,
    )
    labels = label_mod.read_labels("s1", sets_root=sets_root)
    assert labels == {0: "cut"}
    assert any("unrecognised key 'z'" in m for m in messages)
