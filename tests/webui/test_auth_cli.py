"""Auth CLI parity checks.

  - if `whoami` exits 0 with nobody signed in, the signed-out exit code is
    broken and scripts cannot branch on it
  - if `whoami` does not name the signed-in email, the CLI is not a usable
    stand-in for GET /auth/me
  - if `sessions` prints a raw session token rather than a hash prefix, the
    CLI leaks a live credential into terminal scrollback and logs
  - if `logout-all` leaves sessions behind, the CLI cannot revoke
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.webui.auth_cli import main
from apps.webui.server.auth import GoogleIdentity, SessionStore


def _identity() -> GoogleIdentity:
    return GoogleIdentity(
        google_sub="sub-cli",
        email="cli@example.com",
        name="CLI User",
        avatar_url=None,
        refresh_token="refresh-value",
        access_token="access-value",
        access_expires_at="2099-01-01T00:00:00+00:00",
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    state_db.open_rw(path).close()
    return path


def test_whoami_reports_signed_out_with_a_nonzero_exit(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--data-db", str(db_path), "whoami"]) == 1
    assert "not signed in" in capsys.readouterr().out


def test_whoami_names_the_signed_in_account(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    SessionStore(db_path).sign_in(_identity())
    assert main(["--data-db", str(db_path), "whoami"]) == 0
    out = capsys.readouterr().out
    assert "cli@example.com" in out
    assert "sessions=1" in out


def test_sessions_shows_a_hash_prefix_not_the_token(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token = SessionStore(db_path).sign_in(_identity())
    assert main(["--data-db", str(db_path), "sessions"]) == 0
    out = capsys.readouterr().out
    assert token not in out
    assert "refresh_token=yes" in out


def test_logout_all_revokes_every_session(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = SessionStore(db_path)
    token = store.sign_in(_identity())
    assert main(["--data-db", str(db_path), "logout-all"]) == 0
    assert "deleted 1 session" in capsys.readouterr().out
    assert store.resolve(token) is None


def test_missing_state_db_is_an_explicit_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--data-db", str(tmp_path / "absent.db"), "whoami"])
    assert "state DB not found" in str(excinfo.value)
