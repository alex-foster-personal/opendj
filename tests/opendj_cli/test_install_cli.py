"""``opendj install-cli`` PATH helper coverage."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from apps.opendj_cli import EXIT_FAILED, install_cli
from apps.opendj_cli.__main__ import main
from apps.opendj_cli.helptext import LIBRARY_EPILOG, LIST_VERBS_FOOTER


def _payload_tree(tmp_path: Path) -> tuple[Path, Path]:
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "manifest.json").write_text(
        '{"schema":1,"kind":"opendj-engine-payload","identity":{"version":"test"}}',
        encoding="utf-8",
    )
    bundled = payload / "bin" / "opendj"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    bundled.chmod(0o755)
    return payload, bundled


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    return home_dir


def test_install_cli_creates_symlink(home: Path, tmp_path: Path) -> None:
    _payload, bundled = _payload_tree(tmp_path)
    target = home / ".local" / "bin" / "opendj"
    assert install_cli.run([], as_json=False, bundled=bundled, target=target) == 0
    assert target.is_symlink()
    assert target.resolve() == bundled.resolve()


def test_install_cli_is_idempotent(home: Path, tmp_path: Path) -> None:
    _payload, bundled = _payload_tree(tmp_path)
    target = home / ".local" / "bin" / "opendj"
    assert install_cli.run([], as_json=False, bundled=bundled, target=target) == 0
    assert install_cli.run([], as_json=False, bundled=bundled, target=target) == 0
    assert target.resolve() == bundled.resolve()


def test_install_cli_refuses_wrong_existing_symlink(home: Path, tmp_path: Path) -> None:
    _payload, bundled = _payload_tree(tmp_path)
    other = tmp_path / "other" / "opendj"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    target = home / ".local" / "bin" / "opendj"
    target.parent.mkdir(parents=True)
    target.symlink_to(other)
    exit_code = install_cli.run([], as_json=False, bundled=bundled, target=target)
    assert exit_code == EXIT_FAILED
    assert target.resolve() == other.resolve()


def test_install_cli_refuses_existing_regular_file(home: Path, tmp_path: Path) -> None:
    _payload, bundled = _payload_tree(tmp_path)
    target = home / ".local" / "bin" / "opendj"
    target.parent.mkdir(parents=True)
    target.write_text("not a symlink\n", encoding="utf-8")
    exit_code = install_cli.run([], as_json=False, bundled=bundled, target=target)
    assert exit_code == EXIT_FAILED
    assert target.is_file()
    assert not target.is_symlink()


def test_install_cli_refuses_outside_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_cli, "payload_root", lambda: None)
    exit_code = install_cli.run([], as_json=False)
    assert exit_code == EXIT_FAILED


@pytest.mark.requirement("AGENT-05")
def test_help_lists_the_installed_app_verbs() -> None:
    """[if] opendj --help runs [then] it lists the installed-app verbs, [else stop].
    [if] an agent runs opendj --help [then] install-cli and BOTH update verbs
    appear, [else stop].

    AGENT-13: an agent on a machine with only the installed app has no repo to
    read, so a verb that is missing from --help is a verb it cannot find.
    """
    with redirect_stdout(io.StringIO()) as captured:
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
    assert exc.value.code == 0
    printed = captured.getvalue()
    assert "install-cli" in printed
    assert "install-cli" in LIBRARY_EPILOG
    assert "opendj update check" in printed
    assert "opendj update apply" in printed
    assert "opendj update check" in LIBRARY_EPILOG
    assert "opendj update apply" in LIBRARY_EPILOG


@pytest.mark.requirement("AGENT-05")
def test_list_verbs_footer_lists_the_installed_app_verbs() -> None:
    """[if] list-verbs prints its footer [then] it names the installed-app verbs, [else stop].
    [if] an agent runs opendj --list-verbs [then] install-cli and both update
    verbs appear, [else stop]."""
    with redirect_stdout(io.StringIO()) as captured:
        main(["--list-verbs"])
    printed = captured.getvalue()
    assert "install-cli" in printed
    assert "install-cli" in LIST_VERBS_FOOTER
    assert "opendj update check" in printed
    assert "opendj update apply" in printed
    assert "opendj update check" in LIST_VERBS_FOOTER
    assert "opendj update apply" in LIST_VERBS_FOOTER
