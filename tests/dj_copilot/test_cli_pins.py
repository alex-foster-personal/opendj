"""CLI pin flags for play-it (SET-04)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.dj_copilot import cli
from apps.dj_copilot.pinning import PinUnsatisfiableError

pytestmark = pytest.mark.requirement("SET-04")


@pytest.fixture
def tracks_json(tmp_path: Path) -> Path:
    path = tmp_path / "tracks.json"
    path.write_text(json.dumps([
        {"stable_id": "A", "artist": "A", "bpm": 120.0, "key_camelot": "8A", "energy": 5},
        {"stable_id": "B", "artist": "B", "bpm": 124.0, "key_camelot": "9A", "energy": 6},
        {"stable_id": "C", "artist": "C", "bpm": 126.0, "key_camelot": "10A", "energy": 7},
    ]))
    return path


def test_parser_accepts_pin_flags(tracks_json: Path) -> None:
    parser = cli._build_parser()
    args = parser.parse_args([
        "play-it",
        "--playlist", "pl1",
        "--tracks-json", str(tracks_json),
        "--duration", "60",
        "--peak-pin", "A",
        "--peak-pin", "B",
        "--opener-pin", "A",
        "--closer-pin", "C",
    ])
    assert args.peak_pins == ["A", "B"]
    assert args.opener_pins == ["A"]
    assert args.closer_pin == "C"


def test_pin_unsatisfiable_returns_exit_two(
    tracks_json: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _raise(**_kwargs):
        raise PinUnsatisfiableError("pin_not_in_playlist", missing=("Z",))

    monkeypatch.setattr(cli, "play_it", _raise)
    rc = cli.main([
        "--db", str(tmp_path / "state.db"),
        "play-it",
        "--playlist", "pl1",
        "--tracks-json", str(tracks_json),
        "--duration", "60",
        "--peak-pin", "Z",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "pin_not_in_playlist" in err


def test_play_it_forwards_pins_to_goal(
    tracks_json: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def _fake_play_it(**kwargs):
        captured["goal"] = kwargs["goal"]
        result = SimpleNamespace(
            order=["A", "B", "C"],
            per_step_scores=[1.0, 1.0, 1.0],
            per_step_trace=[],
            constraints_unmet=[],
            solve_ms=1.0,
        )
        return 1, result

    monkeypatch.setattr(cli, "play_it", _fake_play_it)
    rc = cli.main([
        "--db", str(tmp_path / "state.db"),
        "play-it",
        "--playlist", "pl1",
        "--tracks-json", str(tracks_json),
        "--duration", "60",
        "--peak-pin", "B",
        "--opener-pin", "A",
        "--closer-pin", "C",
    ])
    assert rc == 0
    goal = captured["goal"]
    assert goal.peak_pins == ("B",)
    assert goal.opener_pins == ("A",)
    assert goal.closer_pin == "C"