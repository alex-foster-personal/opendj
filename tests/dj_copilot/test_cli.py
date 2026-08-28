"""Tests for :mod:`apps.dj_copilot.cli`.

Cover the CLI glue only — play-it + suggest-next subcommands, the
argparse wiring, JSON loading, error paths. The underlying solver and
``suggest_next`` are stubbed so we never build a real beam or touch an
LLM; what we assert is *plumbing* (exit codes, stdout / stderr shape,
DB wiring, Namespace propagation).

Requirement: PLAY-02 (play-it) + AI-01 (suggest-next).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.dj_copilot import cli
from apps.dj_copilot.play_it import InsufficientDataError

pytestmark = pytest.mark.requirement("PLAY-02")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def tracks_json(tmp_path: Path) -> Path:
    """A minimal tracks-json file with three tracks."""
    path = tmp_path / "tracks.json"
    path.write_text(json.dumps([
        {"stable_id": "A", "artist": "A", "bpm": 120.0, "key_camelot": "8A", "energy": 5},
        {"stable_id": "B", "artist": "B", "bpm": 124.0, "key_camelot": "9A", "energy": 6},
        {"stable_id": "C", "artist": "C", "bpm": 126.0, "key_camelot": "10A", "energy": 7},
    ]))
    return path


@pytest.fixture
def state_db(tmp_path: Path) -> Path:
    """A fresh sqlite state-DB path (lazily created by _open_conn)."""
    return tmp_path / "state.db"


# --------------------------------------------------------------------------- #
# _load_tracks_from_json
# --------------------------------------------------------------------------- #


def test_load_tracks_from_json_coerces_numeric_fields(tracks_json: Path) -> None:
    tracks = cli._load_tracks_from_json(tracks_json)
    assert [t.stable_id for t in tracks] == ["A", "B", "C"]
    assert tracks[0].bpm == 120.0
    assert isinstance(tracks[0].energy, int)
    assert tracks[0].key_camelot == "8A"


def test_load_tracks_from_json_accepts_key_alias(tmp_path: Path) -> None:
    """The JSON accepts ``key`` as a fallback for ``key_camelot``."""
    p = tmp_path / "t.json"
    p.write_text(json.dumps([
        {"stable_id": "A", "bpm": 120, "key": "8A", "energy": 5},
    ]))
    out = cli._load_tracks_from_json(p)
    assert out[0].key_camelot == "8A"


def test_load_tracks_from_json_none_fields_stay_none(tmp_path: Path) -> None:
    p = tmp_path / "t.json"
    p.write_text(json.dumps([
        {"stable_id": "A", "bpm": None, "key_camelot": None, "energy": None},
    ]))
    out = cli._load_tracks_from_json(p)
    assert out[0].bpm is None
    assert out[0].energy is None
    # Empty-string key_camelot with fallback to .get("key") → None.
    assert out[0].key_camelot is None


# --------------------------------------------------------------------------- #
# argparse wiring
# --------------------------------------------------------------------------- #


def test_parser_play_it_requires_duration_and_tracks_json(tracks_json: Path) -> None:
    parser = cli._build_parser()
    args = parser.parse_args([
        "play-it",
        "--playlist", "pl1",
        "--tracks-json", str(tracks_json),
        "--duration", "60",
    ])
    assert args.cmd == "play-it"
    assert args.playlist == "pl1"
    assert args.duration == 60
    assert args.floor == 3
    assert args.ceiling == 9
    assert args.overwrite is False


def test_parser_suggest_next_choices_enforced(tracks_json: Path) -> None:
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "suggest-next",
            "--current", "A",
            "--library-json", str(tracks_json),
            "--source", "not-a-choice",
        ])


def test_parser_rejects_unknown_subcommand() -> None:
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["does-not-exist"])


def test_parser_subcommand_required() -> None:
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


# --------------------------------------------------------------------------- #
# play-it command
# --------------------------------------------------------------------------- #


def _fake_step(pos: int, sid: str, hint: str = "bpm-match"):
    """Build a StepTrace-shaped object with just the attrs the CLI touches."""
    return SimpleNamespace(
        position=pos, stable_id=sid, score=1.0, camelot_distance=0,
        bpm_delta_pct=0.0, target_energy=5.0, actual_energy=5.0,
        transition_hint=hint,
    )


def _fake_unmet(pos: int, kind: str = "bpm_window"):
    return SimpleNamespace(position=pos, kind=kind, detail={"extra": "stuff"})


def _fake_result(order: list[str], unmet=None, solve_ms: float = 3.14):
    return SimpleNamespace(
        order=list(order),
        per_step_scores=[1.0] * len(order),
        per_step_trace=[_fake_step(i, sid) for i, sid in enumerate(order)],
        constraints_unmet=unmet or [],
        solve_ms=solve_ms,
    )


def test_play_it_happy_path_prints_order_and_returns_zero(
    tracks_json: Path,
    state_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = {}

    def _fake_play_it(**kwargs):
        called.update(kwargs)
        assert kwargs["playlist_id"] == "pl1"
        assert kwargs["name"] == "PLAY IT"
        assert kwargs["overwrite"] is False
        return 42, _fake_result(["A", "B", "C"])

    monkeypatch.setattr(cli, "play_it", _fake_play_it)

    rc = cli.main([
        "--db", str(state_db),
        "play-it",
        "--playlist", "pl1",
        "--tracks-json", str(tracks_json),
        "--duration", "60",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PLAY IT stored" in out
    assert "play_order_id=42" in out
    assert "tracks=3" in out
    assert "A" in out and "B" in out and "C" in out


def test_play_it_emits_unmet_to_stderr_and_still_returns_zero(
    tracks_json: Path,
    state_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _fake_result(["A", "B"], unmet=[_fake_unmet(1, "camelot_hardcut")])
    monkeypatch.setattr(cli, "play_it", lambda **_: (7, result))

    rc = cli.main([
        "--db", str(state_db),
        "play-it",
        "--playlist", "pl1",
        "--tracks-json", str(tracks_json),
        "--duration", "60",
    ])
    assert rc == 0
    cap = capsys.readouterr()
    assert "constraints_unmet (1)" in cap.err
    assert "camelot_hardcut" in cap.err


def test_play_it_invalid_goal_returns_two(
    tracks_json: Path, state_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """duration_min outside the allowed range surfaces as exit 2."""
    rc = cli.main([
        "--db", str(state_db),
        "play-it",
        "--playlist", "pl1",
        "--tracks-json", str(tracks_json),
        "--duration", "5",  # below minimum (30)
    ])
    assert rc == 2
    assert "error:" in capsys.readouterr().err


def test_play_it_missing_tracks_json_returns_two(
    tmp_path: Path, state_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main([
        "--db", str(state_db),
        "play-it",
        "--playlist", "pl1",
        "--tracks-json", str(tmp_path / "missing.json"),
        "--duration", "60",
    ])
    assert rc == 2
    assert "failed to load tracks-json" in capsys.readouterr().err


def test_play_it_malformed_tracks_json_returns_two(
    tmp_path: Path, state_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    rc = cli.main([
        "--db", str(state_db),
        "play-it",
        "--playlist", "pl1",
        "--tracks-json", str(p),
        "--duration", "60",
    ])
    assert rc == 2


def test_play_it_insufficient_data_returns_one(
    tracks_json: Path,
    state_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _raise(**_):
        raise InsufficientDataError(
            playlist_id="pl1", missing={"bpm": ["A"], "key": [], "energy": []}
        )

    monkeypatch.setattr(cli, "play_it", _raise)

    rc = cli.main([
        "--db", str(state_db),
        "play-it",
        "--playlist", "pl1",
        "--tracks-json", str(tracks_json),
        "--duration", "60",
    ])
    assert rc == 1
    assert "pre-flight failed" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# suggest-next command
# --------------------------------------------------------------------------- #


def test_suggest_next_happy_path(
    tracks_json: Path,
    state_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sug = SimpleNamespace(
        stable_id="B", score=0.875,
        rationale_tags=["bpm_match", "camelot_step_0"],
        rationale_numbers={}, explain_text=None, candidate=None,
    )

    called = {}

    def _fake_suggest_next(**kwargs):
        called.update(kwargs)
        assert kwargs["current_stable_id"] == "A"
        assert kwargs["top_n"] == 10
        # Default --source=manual with no --session-json yields an empty
        # manual context (post-P13-F01); previously context was None.
        assert kwargs["context"] is not None
        assert kwargs["context"].source == "manual"
        assert kwargs["context"].recent == []
        return [sug]

    monkeypatch.setattr(cli, "suggest_next", _fake_suggest_next)

    rc = cli.main([
        "--db", str(state_db),
        "suggest-next",
        "--current", "A",
        "--library-json", str(tracks_json),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "B" in out
    assert "bpm_match" in out


def test_suggest_next_with_explain_prints_explain_block(
    tracks_json: Path,
    state_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sug = SimpleNamespace(
        stable_id="B", score=0.5, rationale_tags=[],
        rationale_numbers={}, explain_text="because vibes",
        candidate=None,
    )
    monkeypatch.setattr(cli, "suggest_next", lambda **_: [sug])

    rc = cli.main([
        "--db", str(state_db),
        "suggest-next",
        "--current", "A",
        "--library-json", str(tracks_json),
        "--explain",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "explain: because vibes" in out


def test_suggest_next_with_session_json_builds_context(
    tracks_json: Path,
    state_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = tmp_path / "session.json"
    session.write_text(json.dumps([
        {"stable_id": "X", "artist": "A", "bpm": 118, "key_camelot": "8A", "energy": 4},
        {"stable_id": "Y", "artist": "B", "bpm": 122, "key": "9A", "energy": 5},
    ]))

    captured = {}

    def _fake(**kwargs):
        captured["ctx"] = kwargs["context"]
        return []

    monkeypatch.setattr(cli, "suggest_next", _fake)

    rc = cli.main([
        "--db", str(state_db),
        "suggest-next",
        "--current", "A",
        "--library-json", str(tracks_json),
        "--session-json", str(session),
    ])
    assert rc == 0
    ctx = captured["ctx"]
    assert ctx is not None
    assert [p.stable_id for p in ctx.recent] == ["X", "Y"]
    # .get("key_camelot") or .get("key") fallback path covered by Y.
    assert ctx.recent[1].key_camelot == "9A"
    assert ctx.source == "manual"


def test_suggest_next_missing_library_json_returns_two(
    tmp_path: Path, state_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main([
        "--db", str(state_db),
        "suggest-next",
        "--current", "A",
        "--library-json", str(tmp_path / "nope.json"),
    ])
    assert rc == 2
    assert "failed to load library-json" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# _open_conn
# --------------------------------------------------------------------------- #


def test_open_conn_applies_play_order_migrations(state_db: Path) -> None:
    """The conn must expose the play-orders tables after opening."""
    conn = cli._open_conn(state_db)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    finally:
        conn.close()
    # Schema names are additive — at least the play_orders root table must exist.
    assert "play_orders" in names or any("play_order" in n for n in names)


@pytest.mark.requirement("AI-01")
def test_suggest_next_uses_source_arg(
    tracks_json: Path,
    state_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI-01 / P13-F01: ``--source`` must be threaded into the context loader.

    Before the fix, ``_cmd_suggest_next`` ignored ``args.source`` entirely
    and always produced either a manual SessionContext (when --session-json
    was given) or context=None (otherwise). The ranker therefore never saw
    phase12 / auto context.
    """
    captured = {}

    # Stub the loader so we can prove it was called with the user's
    # ``--source`` value, without needing a real Phase 12 events table.
    def _fake_loader(*, conn, source, manual=None):
        captured["source"] = source
        captured["manual_passed"] = manual
        from apps.dj_copilot.session_context import SessionContext
        from tests.dj_copilot.conftest import FIXED_NOW
        return SessionContext(
            recent=[], source=source, captured_at=FIXED_NOW
        )

    monkeypatch.setattr(cli, "load_session_context", _fake_loader)
    monkeypatch.setattr(cli, "suggest_next", lambda **kwargs: [])

    rc = cli.main([
        "--db", str(state_db),
        "suggest-next",
        "--current", "A",
        "--library-json", str(tracks_json),
        "--source", "phase12",
    ])
    assert rc == 0
    assert captured["source"] == "phase12", (
        "args.source was not threaded into the context loader"
    )
