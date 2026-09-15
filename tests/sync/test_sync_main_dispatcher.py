"""CLI dispatcher tests for ``python -m apps.sync`` (issue #2764)."""
from __future__ import annotations

import pytest

from apps.sync.__main__ import main

pytestmark = pytest.mark.requirement("SYNC-03")


def test_main_without_args_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "python -m apps.sync" in out
    assert "playlist-diff" in out


def test_match_help_exits_zero() -> None:
    import pytest

    with pytest.raises(SystemExit) as exc:
        main(["match", "--help"])
    assert exc.value.code == 0


def test_unknown_subcommand_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["not-a-real-command"]) == 2
    err = capsys.readouterr().err
    assert "unknown subcommand" in err
