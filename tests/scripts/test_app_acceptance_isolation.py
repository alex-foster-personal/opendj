"""Pre-launch app-data isolation for installed-app acceptance (OPS-54, issue #6260).

[if] resolved app-data is outside the run home [then] exit 2 before launch, [else stop].
"""
from __future__ import annotations

import inspect
import json
import os
import plistlib
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import app_acceptance as acc

pytestmark = pytest.mark.requirement("OPS-54")

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "app-acceptance"
SHA = "2411a8f23190c3d03a8e91ced8ecccf7c5744442"
IDENTIFIER = "com.opendj.desktop.acceptance-fixture"
APP_SUPPORT = Path("Library") / "Application Support"
SHELLS = [acc.TauriShell, acc.ElectronShell]


def _bundle(app: Path, *, electron: bool = False) -> Path:
    """A bundle directory with the real layout markers. Its executable is empty: it cannot run."""
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "Open DJ").write_bytes(b"")
    payload = app / "Contents" / "Resources" / "payload"
    payload.mkdir(parents=True)
    (payload / "manifest.json").write_text(json.dumps({"identity": {"git_sha_full": SHA}}) + "\n")
    if electron:
        (app / "Contents" / "Resources" / "app.asar").write_bytes(b"")
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": IDENTIFIER,
        "CFBundleExecutable": "Open DJ",
        "CFBundleName": "Open DJ",
    }))
    return app


@pytest.fixture
def account_home(tmp_path: Path) -> Path:
    """Stands in for the logged-in account's home. It exists, and it is never the run home."""
    path = tmp_path / "account"
    (path / APP_SUPPORT).mkdir(parents=True)
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "run" / "home"


def test_a_tauri_bundle_is_still_detected(tmp_path: Path) -> None:
    """if a Tauri bundle is refused by shell detection then broken (the overshoot control)"""
    assert type(acc.Shell.detect(_bundle(tmp_path / "Open DJ.app"))) is acc.TauriShell


def test_electron_launch_env_sets_cffixed_user_home(home: Path, tmp_path: Path) -> None:
    """if the Electron launch environment lacks CFFIXED_USER_HOME then broken"""
    app = _bundle(tmp_path / "Open DJ.app", electron=True)
    env = acc.ElectronShell(app).launch_env(home)
    assert env["HOME"] == str(home)
    assert env["CFFIXED_USER_HOME"] == str(home)


@pytest.mark.parametrize("shell_class,expect_cffixed", [(acc.TauriShell, False), (acc.ElectronShell, True)])
def test_the_launcher_command_hands_the_app_every_variable_it_was_given(
    tmp_path: Path, home: Path, shell_class: type[acc.Shell], expect_cffixed: bool,
) -> None:
    """if the launcher command drops a launch environment variable then broken"""
    app = _bundle(tmp_path / "Open DJ.app", electron=shell_class is acc.ElectronShell)
    shell = shell_class(app)
    env = shell.launch_env(home)
    argv = shell.launch_argv(env)
    assert argv[0] == "/usr/bin/open" and argv[-1] == str(app)
    handed = {argv[i + 1] for i, word in enumerate(argv) if word == "--env"}
    assert handed == {f"{key}={value}" for key, value in env.items()}
    assert (f"CFFIXED_USER_HOME={home}" in handed) == expect_cffixed


@pytest.mark.parametrize("shell_class", SHELLS)
def test_each_shell_resolves_inside_the_run_home(
    tmp_path: Path, home: Path, account_home: Path, shell_class: type[acc.Shell],
) -> None:
    """if either shell resolves its app data outside the run home under the built environment then broken"""
    shell = shell_class(_bundle(tmp_path / "Open DJ.app", electron=shell_class is acc.ElectronShell))
    env = shell.launch_env(home)
    assert shell.launch_data_dir(env, account_home) == home / APP_SUPPORT / IDENTIFIER
    assert acc.refuse_unisolated_launch(shell, home, env, account_home) == (home / APP_SUPPORT / IDENTIFIER).resolve()


def test_home_alone_does_not_isolate_electron(tmp_path: Path, home: Path, account_home: Path) -> None:
    """if an Electron data dir under HOME alone is not refused then broken (the #6260 defect)"""
    app = _bundle(tmp_path / "Open DJ.app", electron=True)
    home_only = {"HOME": str(home)}
    electron = acc.ElectronShell(app)
    assert electron.launch_data_dir(home_only, account_home) == account_home / APP_SUPPORT / IDENTIFIER
    with pytest.raises(acc.Refused, match="real app data dir") as refused:
        acc.refuse_unisolated_launch(electron, home, home_only, account_home)
    assert str((account_home / APP_SUPPORT / IDENTIFIER).resolve()) in str(refused.value)
    tauri = acc.TauriShell(_bundle(tmp_path / "Open DJ (tauri).app"))
    assert acc.refuse_unisolated_launch(tauri, home, home_only, account_home) == (home / APP_SUPPORT / IDENTIFIER).resolve()


@pytest.mark.parametrize("shell_class", SHELLS)
def test_an_empty_override_is_not_an_override(tmp_path: Path, account_home: Path, shell_class: type[acc.Shell]) -> None:
    """if an empty home variable resolves to a relative path instead of the account home then broken"""
    shell = shell_class(_bundle(tmp_path / "Open DJ.app", electron=shell_class is acc.ElectronShell))
    empty = {"HOME": "", "CFFIXED_USER_HOME": ""}
    assert shell.launch_data_dir(empty, account_home) == account_home / APP_SUPPORT / IDENTIFIER


def test_a_data_dir_outside_the_run_home_is_refused(tmp_path: Path, home: Path, account_home: Path) -> None:
    """if a data dir outside the run home, or one sharing only its name prefix, is not refused then broken"""
    shell = acc.TauriShell(_bundle(tmp_path / "Open DJ.app"))
    home.mkdir(parents=True)
    elsewhere = [
        home.parent,
        Path(str(home) + "-other"),
        tmp_path / "unrelated",
    ]
    for other_home in elsewhere:
        with pytest.raises(acc.Refused, match="outside the run home"):
            acc.refuse_unisolated_launch(shell, home, {"HOME": str(other_home)}, account_home)


def test_a_symlink_into_the_real_library_is_refused_at_launch_time(tmp_path: Path, home: Path, account_home: Path) -> None:
    """if a data dir that leaves the run home through a symlink is not refused at launch time then broken"""
    shell = acc.TauriShell(_bundle(tmp_path / "Open DJ.app"))
    env = shell.launch_env(home)
    real_library = account_home / APP_SUPPORT / "com.opendj.desktop"
    real_library.mkdir()
    (account_home / APP_SUPPORT / IDENTIFIER).symlink_to(real_library, target_is_directory=True)
    assert acc.refuse_unisolated_launch(shell, home, env, account_home) == (home / APP_SUPPORT / IDENTIFIER).resolve()
    home.mkdir(parents=True)
    (home / "Library").symlink_to(account_home / "Library", target_is_directory=True)
    with pytest.raises(acc.Refused, match="real app data dir") as refused:
        acc.refuse_unisolated_launch(shell, home, env, account_home)
    assert str(real_library.resolve()) in str(refused.value)


def test_a_symlink_that_leaves_the_run_home_is_refused(tmp_path: Path, home: Path, account_home: Path) -> None:
    """if a data dir that is lexically inside the run home but resolves outside it is not refused then broken"""
    shell = acc.TauriShell(_bundle(tmp_path / "Open DJ.app"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    home.mkdir(parents=True)
    (home / "Library").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(acc.Refused, match="outside the run home"):
        acc.refuse_unisolated_launch(shell, home, shell.launch_env(home), account_home)


def test_a_run_home_inside_the_real_data_dir_is_refused(tmp_path: Path, account_home: Path) -> None:
    """if a data dir at or inside the real app data dir is not refused then broken"""
    shell = acc.TauriShell(_bundle(tmp_path / "Open DJ.app"))
    real = account_home / APP_SUPPORT / IDENTIFIER
    inside_real = real / "acceptance" / "home"
    inside_real.mkdir(parents=True)
    with pytest.raises(acc.Refused, match="real app data dir"):
        acc.refuse_unisolated_launch(shell, inside_real, shell.launch_env(inside_real), account_home)


def test_a_data_dir_inside_an_existing_run_home_is_allowed(tmp_path: Path, home: Path, account_home: Path) -> None:
    """if a data dir inside an existing run home is refused then broken (the overshoot control)"""
    shell = acc.TauriShell(_bundle(tmp_path / "Open DJ.app"))
    (home / APP_SUPPORT / IDENTIFIER).mkdir(parents=True)
    (account_home / APP_SUPPORT / IDENTIFIER).mkdir()
    allowed = acc.refuse_unisolated_launch(shell, home, shell.launch_env(home), account_home)
    assert allowed == (home / APP_SUPPORT / IDENTIFIER).resolve()
    assert allowed.is_dir()


def test_boot_refuses_at_launch_time_before_it_launches(tmp_path: Path, home: Path, account_home: Path) -> None:
    """if the launch-time check is missing then broken"""
    app = _bundle(tmp_path / "Open DJ.app")
    shell = acc.TauriShell(app)
    shutil.rmtree(app)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    home.mkdir(parents=True)
    (home / "Library").symlink_to(elsewhere, target_is_directory=True)
    run = acc.Run(shell=shell, fixture=acc.Fixture.load(FIXTURE), home=home, env=shell.launch_env(home),
                  account_home=account_home, expect_sha=SHA, stems_timeout_s=1.0, lyrics_timeout_s=1.0)
    with pytest.raises(acc.Refused, match="outside the run home"):
        acc._boot(run)
    assert run.launched_at == 0.0


def test_the_check_runs_before_the_run_home_and_before_the_launch() -> None:
    """if the launch-time check runs after the launch, or the first check after the run home is made, then broken"""
    boot = inspect.getsource(acc._boot)
    check = boot.index("refuse_unisolated_launch(run.shell, run.home, run.env, run.account_home)")
    assert check < boot.index("run.launched_at = ") < boot.index("run.shell.launch(run.env)")
    orchestration = inspect.getsource(acc._begin_acceptance)
    first = orchestration.index("refuse_unisolated_launch(shell, home, env, account_home)")
    assert orchestration.index("Shell.detect(app)") < first
    assert first < orchestration.index("_real_store_stat(shell)") < orchestration.index("_fresh_home(")
    assert "env=env, account_home=account_home" in orchestration


def test_the_account_home_comes_from_the_user_database_not_from_home(tmp_path: Path) -> None:
    """if the account home comes from HOME instead of the user database then broken"""
    decoy = str(tmp_path / "decoy-home")
    expected = pwd.getpwuid(os.getuid()).pw_dir
    assert expected != decoy
    proc = subprocess.run(
        [sys.executable, "-c", "from scripts import app_acceptance as acc; print(acc._account_home())"],
        capture_output=True, text=True, timeout=60, check=False, cwd=REPO,
        env={**os.environ, "HOME": decoy})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


CANONICAL_WEBKIT_DATA_DIR = Path("/Users/USER/Library/Application Support/com.opendj.desktop")
CANONICAL_WEBKIT_ID_HEX = "40ad07d8cf036a9053b023817c437e74"
CANONICAL_WEBKIT_UUID = "40AD07D8-CF03-6A90-53B0-23817C437E74"


@pytest.mark.requirement("OPS-54")
def test_webkit_data_store_identifier_matches_rust() -> None:
    """[if] the Python hash drifts from the Tauri helper [then] broken, [else stop]."""
    assert acc.webkit_data_store_identifier(CANONICAL_WEBKIT_DATA_DIR) == bytes.fromhex(CANONICAL_WEBKIT_ID_HEX)
    assert acc.webkit_data_store_uuid(CANONICAL_WEBKIT_DATA_DIR) == CANONICAL_WEBKIT_UUID


@pytest.mark.requirement("OPS-54")
def test_webkit_data_store_identifier_is_stable_and_distinct(tmp_path: Path) -> None:
    """[if] two data dirs share a store id [then] broken, [else stop]."""
    first = tmp_path / "one"
    second = tmp_path / "two"
    assert acc.webkit_data_store_identifier(first) == acc.webkit_data_store_identifier(first)
    assert acc.webkit_data_store_identifier(first) != acc.webkit_data_store_identifier(second)


@pytest.mark.requirement("OPS-54")
def test_real_webkit_store_digest_tracks_default_and_real_uuid_only(
    tmp_path: Path, account_home: Path,
) -> None:
    """[if] the acceptance run store changes the real-store digest [then] broken, [else stop]."""
    real_data = account_home / APP_SUPPORT / IDENTIFIER
    real_data.mkdir(parents=True)
    digest = acc.real_webkit_store_digest(account_home, IDENTIFIER, real_data)
    assert digest["webkit.Default"] is None
    assert digest[f"webkit.{acc.webkit_data_store_uuid(real_data)}"] is None

    default_root = account_home / "Library" / "WebKit" / IDENTIFIER / "WebsiteData" / "Default"
    (default_root / "salt").mkdir(parents=True)
    (default_root / "salt" / "file.txt").write_bytes(b"before")
    after_default = acc.real_webkit_store_digest(account_home, IDENTIFIER, real_data)
    assert after_default["webkit.Default"] is not None
    assert after_default[f"webkit.{acc.webkit_data_store_uuid(real_data)}"] is None

    run_data = tmp_path / "run-data"
    run_uuid = acc.webkit_data_store_uuid(run_data)
    run_store = account_home / "Library" / "WebKit" / IDENTIFIER / "WebsiteData" / run_uuid
    run_store.mkdir(parents=True)
    (run_store / "noise.txt").write_bytes(b"acceptance-only")
    unchanged = acc.real_webkit_store_digest(account_home, IDENTIFIER, real_data)
    assert unchanged == after_default


@pytest.mark.requirement("OPS-54")
def test_judge_real_store_fails_when_webkit_default_changes(account_home: Path) -> None:
    """[if] Default WebKit bytes change and the guard passes [then] broken, [else stop]."""
    real_data = account_home / APP_SUPPORT / IDENTIFIER
    real_data.mkdir(parents=True)
    default_root = account_home / "Library" / "WebKit" / IDENTIFIER / "WebsiteData" / "Default"
    default_root.mkdir(parents=True)
    (default_root / "x.txt").write_bytes(b"one")
    before = acc.real_webkit_store_digest(account_home, IDENTIFIER, real_data)
    (default_root / "x.txt").write_bytes(b"two")
    after = acc.real_webkit_store_digest(account_home, IDENTIFIER, real_data)
    check = acc.judge_real_store(before, None, after, None)
    assert check.verdict == acc.FAIL
    assert "webkit.Default" in str(check.numbers.get("after"))


@pytest.mark.requirement("OPS-54")
def test_judge_real_store_passes_when_only_the_run_uuid_store_appears(
    tmp_path: Path, account_home: Path,
) -> None:
    """[if] a new acceptance UUID store fails the guard [then] broken, [else stop]."""
    real_data = account_home / APP_SUPPORT / IDENTIFIER
    real_data.mkdir(parents=True)
    before = acc.real_webkit_store_digest(account_home, IDENTIFIER, real_data)
    run_uuid = acc.webkit_data_store_uuid(tmp_path / "acceptance-data")
    run_store = account_home / "Library" / "WebKit" / IDENTIFIER / "WebsiteData" / run_uuid
    run_store.mkdir(parents=True, exist_ok=True)
    (run_store / "local").mkdir(parents=True)
    (run_store / "local" / "data.bin").write_bytes(b"run")
    after = acc.real_webkit_store_digest(account_home, IDENTIFIER, real_data)
    check = acc.judge_real_store(before, None, after, None)
    assert check.verdict == acc.PASS
