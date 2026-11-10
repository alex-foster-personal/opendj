"""CLI surface tests for apps.spotify.__main__."""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import schema as state_schema
from apps.spotify.__main__ import main as cli_main


@pytest.fixture
def state_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path, isolation_level=None)
    state_schema.apply_migrations(conn)
    conn.close()
    monkeypatch.setattr("apps.shared.paths.STATE_DB", path)
    monkeypatch.setattr("apps.shared.state.paths.STATE_DB", path)
    return path


@pytest.mark.requirement("CAT-01")
def test_cli_help_exits_zero(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli_main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "import" in out and "rematch" in out


@pytest.mark.requirement("CAT-01")
def test_cli_import_rejects_invalid_url(capsys: pytest.CaptureFixture) -> None:
    rc = cli_main(["import", "not-a-playlist"])
    assert rc == 3
    assert "ERROR" in capsys.readouterr().err


@pytest.mark.requirement("CAT-01")
def test_cli_import_live_without_flag_refuses(capsys: pytest.CaptureFixture) -> None:
    rc = cli_main(["import", "37i9dQZF1DXcBWIGoYBM5M", "--live"])
    assert rc == 3
    assert "i-understand-the-risks" in capsys.readouterr().err


@pytest.mark.requirement("CAT-01")
def test_cli_rematch_with_no_pending(
    capsys: pytest.CaptureFixture,
    state_db_path: Path,
) -> None:
    rc = cli_main(["rematch", "--playlist-id", "37i9dQZF1DXcBWIGoYBM5M"])
    assert rc == 0
    assert "rematch" in capsys.readouterr().out


@pytest.mark.requirement("CAT-01")
def test_cli_rematch_live_without_flag_refuses(
    capsys: pytest.CaptureFixture,
) -> None:
    rc = cli_main(
        [
            "rematch",
            "--playlist-id",
            "37i9dQZF1DXcBWIGoYBM5M",
            "--live",
        ]
    )
    assert rc == 3
    assert "i-understand-the-risks" in capsys.readouterr().err


@pytest.mark.requirement("CAT-01")
def test_cli_rematch_live_typed_confirm_mismatch_refuses(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    state_db_path: Path,
) -> None:
    """Typing the wrong playlist id at the confirm prompt must abort the live write."""
    monkeypatch.setattr("sys.stdin", io.StringIO("wrong-id\n"))
    rc = cli_main(
        [
            "rematch",
            "--playlist-id",
            "37i9dQZF1DXcBWIGoYBM5M",
            "--live",
            "--i-understand-the-risks",
        ]
    )
    assert rc == 3
    err = capsys.readouterr().err
    assert "typed confirmation mismatch" in err


@pytest.mark.requirement("CAT-01")
def test_cli_rematch_live_eof_at_prompt_refuses(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    state_db_path: Path,
) -> None:
    """EOF at the typed-confirm prompt must abort rather than proceed."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = cli_main(
        [
            "rematch",
            "--playlist-id",
            "37i9dQZF1DXcBWIGoYBM5M",
            "--live",
            "--i-understand-the-risks",
        ]
    )
    assert rc == 3
    assert "aborted" in capsys.readouterr().err


@pytest.mark.requirement("CAT-02")
def test_cli_import_default_max_tracks_is_safe() -> None:
    """Regression (codex P09): a bare ``spotify import <id>`` MUST NOT
    default to unlimited. The parser's default for --max-tracks must be a
    finite, conservative integer so that an accidental bare invocation can
    never import every track in a playlist against the live state DB.
    Opting out requires an explicit --max-tracks 0, --unlimited, or --bulk.
    """
    from apps.spotify.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["import", "37i9dQZF1DXcBWIGoYBM5M"])

    # Must not default to "unlimited" (None) or 0.
    assert args.max_tracks is not None, "default --max-tracks must not be None (unlimited); got None"
    assert isinstance(args.max_tracks, int) and args.max_tracks > 0, (
        f"default --max-tracks must be a positive int; got {args.max_tracks!r}"
    )
    # Conservative: default cap should be small enough to prevent a
    # catastrophic accidental bulk import.
    assert args.max_tracks <= 100, (
        f"default --max-tracks ({args.max_tracks}) is not conservative; "
        "expected a small cap (<= 100) per CAT-02 safe-default policy"
    )

    # Explicit opt-outs remain available.
    assert parser.parse_args(["import", "id", "--unlimited"]).unlimited is True
    assert parser.parse_args(["import", "id", "--max-tracks", "0"]).max_tracks == 0


@pytest.mark.requirement("SPOTIFY-11967")
def test_cli_login_no_browser_skips_browser_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the operator used --no-browser [then] login never opens the authorize URL, [else stop]."""
    opens: list[str] = []
    captured: dict[str, bool] = {}

    class _Flow:
        def start(self) -> str:
            return "https://accounts.spotify.com/authorize?client_id=x"

    def _wait(
        flow: object,
        url: str,
        *,
        open_browser: bool,
        **_: object,
    ) -> dict[str, str | None]:
        captured["open_browser"] = open_browser
        return {"phase": "connected", "detail": None}

    monkeypatch.setattr("apps.spotify.account.ConnectFlow", lambda: _Flow())
    monkeypatch.setattr("apps.spotify.account.wait_pkce_connect", _wait)
    monkeypatch.setattr("apps.spotify.account.open_pkce_authorize_url", lambda url: opens.append(url))
    assert cli_main(["login", "--no-browser"]) == 0
    assert opens == []
    assert captured.get("open_browser") is False


@pytest.mark.requirement("SPOT-6555")
@pytest.mark.parametrize(("body", "problem"), [(b"{not json", "not JSON"), (b'{"client_id": "\xff"}', "not UTF-8")])
def test_cli_login_reports_an_unreadable_client_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, body: bytes, problem: str
) -> None:
    """[if] the saved client id file is damaged [then] login prints SPOTIFY_CLIENT_ID_UNREADABLE and exits 3, [else stop].

    Otherwise the CLI dies with a traceback instead of the diagnostic the panel shows."""
    from apps.spotify import account

    saved = tmp_path / "spotify-client.json"
    saved.write_bytes(body)
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.setattr(account, "CLIENT_ID_PATH", saved)
    assert cli_main(["login", "--no-browser"]) == 3
    err = capsys.readouterr().err
    assert "SPOTIFY_CLIENT_ID_UNREADABLE" in err and problem in err
