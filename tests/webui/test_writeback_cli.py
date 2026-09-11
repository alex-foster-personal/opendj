"""CLI parity regressions for fail-closed playlist writeback."""
from __future__ import annotations

import pytest

from apps.webui import writeback_cli


def _apply_arguments(*extra: str) -> list[str]:
    return [
        "apply", "source-1", "--vendor", "djay", "--target-mode", "live",
        "--target-path", "/library/MediaLibrary.db", "--target-id", "native-9",
        "--plan-token", "reviewed-token", *extra,
    ]


def test_cli_apply_dry_run_uses_the_http_preview_contract(monkeypatch) -> None:
    requests: list[tuple[str, str, str, dict]] = []
    monkeypatch.setenv("MUSIC_DJ_BACKEND_PORT", "18697")
    monkeypatch.setattr(
        writeback_cli,
        "_request",
        lambda base, method, path, body: requests.append((base, method, path, body)) or {"applied": False},
    )

    assert writeback_cli.main(_apply_arguments("--dry-run")) == 0
    assert requests == [(
        "http://127.0.0.1:18697/api/v1", "POST", "/playlists/source-1/writeback/apply",
        {"vendor": "djay", "target_mode": "live", "target_path": "/library/MediaLibrary.db", "target_id": "native-9", "plan_token": "reviewed-token", "dry_run": True, "confirmed": False},
    )]


def test_cli_apply_refuses_a_live_write_without_confirmation(capsys) -> None:
    # --base-url is the CLI's own documented way to name the daemon, and it is
    # what this test uses so the subject stays the confirmation guard rather
    # than port resolution. Previously it passed no base URL and no port, so it
    # fell through to MUSIC_DJ_BACKEND_PORT and only passed when
    # tests/webui/test_server_port_config.py had already leaked that variable
    # into the process -- an ordering assumption that parallel execution does
    # not honour, and that failed serially too when the file ran on its own.
    arguments = ["--base-url", "http://127.0.0.1:18697/api/v1", *_apply_arguments()]

    assert writeback_cli.main(arguments) == 2
    assert "--confirm or --dry-run" in capsys.readouterr().err

pytestmark = pytest.mark.rb_parity
