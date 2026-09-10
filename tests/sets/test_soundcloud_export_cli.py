"""CLI contract for ``python -m apps.sets soundcloud-export`` (SET-06a)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from apps.sets import __main__ as cli
from apps.sets import paths as sets_paths_mod
from apps.sets.soundcloud_export import LICENSING_REMINDER, build_soundcloud_export
from tests.sets.test_soundcloud_export import THREE_TRACK_TIMELINE, _seed_session

_TIMESTAMP_LINE = re.compile(r"(?m)^\d+:\d{2}(?::\d{2})? ")


@pytest.fixture
def seeded_cli(sets_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _seed_session(sets_root, timeline=THREE_TRACK_TIMELINE)
    monkeypatch.setattr(sets_paths_mod, "SETS_DIR", sets_root)
    return sets_root


@pytest.mark.requirement("SET-06a")
def test_soundcloud_export_subcommand_is_registered() -> None:
    parser = cli._build_parser()
    sub_actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    choices = sub_actions[0].choices
    assert choices is not None
    assert "soundcloud-export" in choices


@pytest.mark.requirement("SET-06a")
def test_without_acknowledge_prints_reminder_not_comment(
    seeded_cli: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = seeded_cli / "comment.txt"
    rc = cli.main(["soundcloud-export", "s1", "--out", str(out_path)])
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert rc == 2
    assert LICENSING_REMINDER in combined
    assert _TIMESTAMP_LINE.search(combined) is None
    assert "First Tune" not in combined
    assert not out_path.exists()


@pytest.mark.requirement("SET-06a")
def test_with_acknowledge_prints_reminder_before_comment(
    seeded_cli: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["soundcloud-export", "s1", "--acknowledge-rights"])
    captured = capsys.readouterr()
    assert rc == 0
    out = captured.out
    assert LICENSING_REMINDER in out
    match = _TIMESTAMP_LINE.search(out)
    assert match is not None
    assert out.index(LICENSING_REMINDER) < match.start()
    library = build_soundcloud_export("s1", sets_root=seeded_cli)
    assert library.comment in out
    assert "First Tune" in out


@pytest.mark.requirement("SET-06a")
def test_json_with_ack_includes_sentinels_and_library_comment(
    seeded_cli: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["soundcloud-export", "s1", "--acknowledge-rights", "--json"])
    assert rc == 0
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    library = build_soundcloud_export("s1", sets_root=seeded_cli)
    assert payload["kind"] == "metadata_only"
    assert payload["audio_upload"] == "not_offered"
    assert payload["takeover"] == "not_offered"
    assert payload["rights_position"] == "unsettled"
    assert payload["licensing_reminder"] == LICENSING_REMINDER
    assert payload["comment"] == library.comment
    assert raw.index("licensing_reminder") < raw.index('"comment"')
    assert "upload_url" not in payload
    assert "player_url" not in payload


@pytest.mark.requirement("SET-06a")
def test_out_writes_comment_only_after_ack(
    seeded_cli: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = seeded_cli.parent / "pasted-comment.txt"
    rc = cli.main(
        [
            "soundcloud-export",
            "s1",
            "--acknowledge-rights",
            "--out",
            str(out_path),
        ]
    )
    assert rc == 0
    capsys.readouterr()
    library = build_soundcloud_export("s1", sets_root=seeded_cli)
    assert out_path.read_text(encoding="utf-8") == library.comment
    assert out_path.parent != seeded_cli / "s1"


@pytest.mark.requirement("SET-06a")
def test_missing_session_exits_2_without_fake_tracklist(
    sets_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sets_paths_mod, "SETS_DIR", sets_root)
    rc = cli.main(["soundcloud-export", "nope", "--acknowledge-rights"])
    captured = capsys.readouterr()
    assert rc == 2
    combined = captured.out + captured.err
    assert "nope" in combined
    assert _TIMESTAMP_LINE.search(combined) is None
    assert "First Tune" not in combined
