"""Tests for :mod:`apps.sets.replay` (Plan 12-03)."""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from apps.sets import replay as replay_mod
from apps.sets.manifest import Manifest, write_manifest


def _seed(sets_root: Path, session_id: str = "s1") -> Path:
    sess = sets_root / session_id
    sess.mkdir(parents=True, exist_ok=True)
    write_manifest(
        sess,
        Manifest(
            session_id=session_id,
            started_at="2026-04-17T21:30:00+00:00",
            ended_at="2026-04-17T23:00:00+00:00",
            capture_device="BlackHole 2ch",
            event_count=10,
            deck_sources=["djay_monitor"],
            mp3_segments=[],
        ),
    )
    transitions = [
        {"idx": 0, "t_change_s": 100.0, "from_deck": "A", "to_deck": "B",
         "from_track": "t1", "to_track": "t2", "predicted_class": "cut",
         "confidence": 0.9, "model_version": "rules-v0",
         "features": {"overlap_s": 0.1}},
        {"idx": 1, "t_change_s": 500.0, "from_deck": "B", "to_deck": "A",
         "from_track": "t2", "to_track": "t3", "predicted_class": "blend",
         "confidence": 0.75, "model_version": "rules-v0",
         "features": {"overlap_s": 12.0}},
        {"idx": 2, "t_change_s": 900.0, "from_deck": "A", "to_deck": "B",
         "from_track": "t3", "to_track": "t4", "predicted_class": "quick_double",
         "confidence": 0.85, "model_version": "rules-v0",
         "features": {"overlap_s": 1.0}},
    ]
    (sess / "transitions.jsonl").write_text(
        "\n".join(json.dumps(t) for t in transitions) + "\n"
    )
    return sess


@pytest.mark.requirement("SET-03")
def test_parse_since_accepts_shortforms():
    assert replay_mod.parse_since("45m") == 45 * 60
    assert replay_mod.parse_since("1h30m") == 90 * 60
    assert replay_mod.parse_since("90s") == 90
    assert replay_mod.parse_since("120") == 120
    assert replay_mod.parse_since(None) is None


@pytest.mark.requirement("SET-03")
def test_parse_since_rejects_garbage():
    with pytest.raises(ValueError):
        replay_mod.parse_since("not-a-time")


@pytest.mark.requirement("SET-03")
def test_replay_jsonl_prints_all_transitions(sets_root: Path, capsys):
    _seed(sets_root)
    exit_code = replay_mod.replay(
        "s1",
        output_format="jsonl",
        sets_root=sets_root,
        ensure_transitions=False,
    )
    assert exit_code == 0
    captured = capsys.readouterr().out
    lines = [line for line in captured.splitlines() if line.strip()]
    assert len(lines) == 3
    assert [json.loads(line)["idx"] for line in lines] == [0, 1, 2]


@pytest.mark.requirement("SET-03")
def test_replay_since_filters_recent(sets_root: Path, capsys):
    _seed(sets_root)
    exit_code = replay_mod.replay(
        "s1",
        output_format="jsonl",
        since="300s",  # only the last 300s, anchor = latest change 900s
        sets_root=sets_root,
        ensure_transitions=False,
    )
    assert exit_code == 0
    captured = capsys.readouterr().out
    lines = [line for line in captured.splitlines() if line.strip()]
    # idx=1 at 500s and idx=2 at 900s; cutoff = 900 - 300 = 600 -> only idx=2
    assert len(lines) == 1
    assert json.loads(lines[0])["idx"] == 2


@pytest.mark.requirement("SET-03")
def test_replay_class_filter(sets_root: Path, capsys):
    _seed(sets_root)
    replay_mod.replay(
        "s1",
        output_format="jsonl",
        class_filter="blend",
        sets_root=sets_root,
        ensure_transitions=False,
    )
    captured = capsys.readouterr().out
    lines = [line for line in captured.splitlines() if line.strip()]
    assert [json.loads(line)["predicted_class"] for line in lines] == ["blend"]


@pytest.mark.requirement("SET-03")
def test_replay_rich_output_renders_table(sets_root: Path):
    _seed(sets_root)
    buf = StringIO()
    console = Console(file=buf, width=200, force_terminal=False)
    exit_code = replay_mod.replay(
        "s1",
        output_format="rich",
        sets_root=sets_root,
        ensure_transitions=False,
        console=console,
    )
    assert exit_code == 0
    rendered = buf.getvalue()
    assert "Transitions" in rendered
    assert "cut" in rendered
    assert "blend" in rendered
    assert "quick_double" in rendered


@pytest.mark.requirement("SET-03")
def test_replay_missing_session_returns_nonzero(sets_root: Path):
    exit_code = replay_mod.replay("nope", sets_root=sets_root)
    assert exit_code == 2
