"""Smoke tests for the ``python -m apps.sets`` argparse entry."""
from __future__ import annotations

import json

import pytest

from apps.sets import __main__ as cli
from apps.sets import paths as sets_paths_mod
from apps.sets.manifest import Manifest, write_manifest


@pytest.mark.requirement("SET-01")
def test_parser_exposes_expected_subcommands():
    parser = cli._build_parser()
    # argparse stores sub-parsers in actions[?].choices
    sub_actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    assert sub_actions, "expected at least one subparsers action"
    choices = set(sub_actions[0].choices.keys())
    assert {
        "start", "stop", "resume", "status", "list",
        "prune", "classify", "label", "train", "replay",
    } <= choices


@pytest.mark.requirement("SET-01")
def test_status_command_runs_with_empty_tree(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(sets_paths_mod, "SETS_DIR", tmp_path / "empty")
    rc = cli.main(["status"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert '"active": false' in captured


@pytest.mark.requirement("SET-01")
def test_list_command_handles_empty_tree(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(sets_paths_mod, "SETS_DIR", tmp_path / "empty")
    # list uses SetsState not sessions_mod, but still needs the DB path:
    from apps.sets import state as state_mod
    monkeypatch.setattr(
        state_mod.sets_paths, "SETS_DB", tmp_path / "sets.db"
    )
    rc = cli.main(["list"])
    assert rc == 0
    assert "no sessions recorded yet" in capsys.readouterr().out


@pytest.mark.requirement("SET-03")
def test_replay_command_renders_seeded_session(tmp_path, monkeypatch, capsys):
    sets_root = tmp_path / "sets"
    sets_root.mkdir()
    sess = sets_root / "s1"
    sess.mkdir()
    write_manifest(
        sess,
        Manifest(
            session_id="s1",
            started_at="2026-04-17T21:30:00+00:00",
            ended_at="2026-04-17T22:15:00+00:00",
            capture_device="BlackHole 2ch",
            event_count=3,
            deck_sources=["djay_monitor"],
        ),
    )
    (sess / "transitions.jsonl").write_text(
        json.dumps({
            "idx": 0, "t_start_s": 0.0, "t_change_s": 10.0, "t_end_s": 12.0,
            "from_deck": "A", "to_deck": "B", "from_track": "t1",
            "to_track": "t2", "predicted_class": "cut", "confidence": 0.9,
            "model_version": "rules-v0",
            "features": {"overlap_s": 0.1},
        }) + "\n"
    )
    monkeypatch.setattr(sets_paths_mod, "SETS_DIR", sets_root)
    rc = cli.main(["replay", "s1", "--format", "jsonl"])
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out.strip().splitlines()[0])["predicted_class"] == "cut"


@pytest.mark.requirement("SET-01")
def test_prune_command_reports_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sets_paths_mod, "SETS_DIR", tmp_path / "empty")
    rc = cli.main(["prune", "--retention-days", "5"])
    assert rc == 0
    assert "no segments older" in capsys.readouterr().out
