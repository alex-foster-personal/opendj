"""LIGHT-10573 stage replay CLI and Python twin (issue #9898).

[if] replay_query or opendj lighting replay diverges from window.musicDjToolsLightingStage.query() [then] fail, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.lighting.stage_replay import replay_query
from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED, lighting_cli

pytestmark = [pytest.mark.requirement("LIGHT-10573")]

VECTORS = Path(__file__).parent / "fixtures" / "stage-replay-vectors.json"


def _vectors() -> dict:
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def test_twin_matches_vectors() -> None:
    """[if] replay_query disagrees with the shared vectors [then] stop."""
    data = _vectors()
    replay_doc = data["replay_doc"]
    bare = data["bare_frames"]
    for entry in data["queries"]:
        text = json.dumps(replay_doc) if entry["which"] == "replay_doc" else json.dumps(bare)
        got = replay_query(text, at_s=entry["at_s"], name="vectors.json")
        assert got == entry["expected"]


def test_twin_errors_match_vectors() -> None:
    """[if] Python twin error text differs from vectors [then] stop."""
    data = _vectors()
    for entry in data["errors"]:
        with pytest.raises(ValueError) as exc:
            replay_query(entry["text"], at_s=0, name="vectors.json")
        assert exc.value.args[0] == entry["message"]


def test_cli_replay_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] CLI --json differs from vectors at the same time [then] stop."""
    data = _vectors()
    path = tmp_path / "vectors.json"
    path.write_text(json.dumps(data["replay_doc"]), encoding="utf-8")
    target = next(q for q in data["queries"] if q["which"] == "replay_doc" and q["at_s"] == 1)
    code = lighting_cli.run(["replay", str(path), "--at", "1", "--json"])
    assert code == EXIT_CONFIRMED
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed == target["expected"]


def test_cli_replay_text_and_failures(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] text summary or failure paths are wrong [then] stop."""
    data = _vectors()
    path = tmp_path / "vectors.json"
    path.write_text(json.dumps(data["replay_doc"]), encoding="utf-8")
    code = lighting_cli.run(["replay", str(path), "--at", "0"])
    assert code == EXIT_CONFIRMED
    out = capsys.readouterr().out
    assert "replay vectors.json:" in out
    assert "frame" in out

    missing = tmp_path / "missing.json"
    code = lighting_cli.run(["replay", str(missing)])
    assert code == EXIT_FAILED
    err = capsys.readouterr().err
    assert "cannot read" in err

    empty = tmp_path / "empty.json"
    empty.write_text('{"frames":[]}', encoding="utf-8")
    code = lighting_cli.run(["replay", str(empty)])
    assert code == EXIT_FAILED
    assert "frame list is empty" in capsys.readouterr().err

    bad_frames = tmp_path / "bad-frames.json"
    bad_frames.write_text('{"frames":"x"}', encoding="utf-8")
    code = lighting_cli.run(["replay", str(bad_frames)])
    assert code == EXIT_FAILED
    assert "frames must be a non-empty array" in capsys.readouterr().err
