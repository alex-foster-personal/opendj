"""The engine payload reaches the packed Electron app on every host (OPS-63).

[if] a staged payload is installed into a packed app [then] it arrives complete and executable, [else stop].

[if] a packed Electron app's resources/payload is the placeholder and a staged payload has
     executable bin/opendj-engine and bin/odj-audio [then] the copy puts both, executable, in
     the packed tree with symlinks kept as symlinks, [else stop].
[if] the packed payload dir holds anything but the placeholder README, or the stage lacks the
     manifest or an executable entry point [then] the install refuses and deletes nothing, [else stop].
[if] the staged manifest was built from any commit but the checkout's HEAD, or names none [then] the
     install refuses and deletes nothing, so a stale stage never ships an old engine, [else stop].
[if] the build host is Linux [then] the payload builder still stages odj-audio and ffmpeg, the
     beatgrid runner install keeps the PyTorch index, and the Mach-O bars report UNKNOWN rather
     than a verdict; on macOS each of those stays as it was, [else stop].

-Claude
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from scripts import build_engine_payload, build_ffmpeg_lgpl, electron_payload, payload_beatgrid

pytestmark = pytest.mark.requirement("OPS-63")

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="executable bits and symlinks are POSIX semantics")


def _stage(root: Path, *, audio_executable: bool = True) -> Path:
    staged = root / "payload-stage"
    (staged / "bin").mkdir(parents=True)
    (staged / "runtime" / "bin").mkdir(parents=True)
    (staged / "manifest.json").write_text("{}", encoding="utf-8")
    for name, executable in (("opendj-engine", True), ("odj-audio", audio_executable)):
        path = staged / "bin" / name
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755 if executable else 0o644)
    (staged / "runtime" / "bin" / "python3.11").write_text("interp", encoding="utf-8")
    (staged / "runtime" / "bin" / "python3").symlink_to("python3.11")
    return staged


def _placeholder(root: Path) -> Path:
    resources = root / "linux-unpacked" / "resources"
    (resources / "payload").mkdir(parents=True)
    (resources / "payload" / "README.md").write_text("placeholder\n", encoding="utf-8")
    return resources


@POSIX_ONLY
def test_linux_install_copies_executables_and_keeps_symlinks(tmp_path: Path) -> None:
    resources = _placeholder(tmp_path)
    target = electron_payload.install_payload(resources, _stage(tmp_path), "linux")
    assert target == resources / "payload"
    for relative in electron_payload.REQUIRED_EXECUTABLES:
        mode = (target / relative).stat().st_mode
        assert stat.S_ISREG(mode) and mode & stat.S_IXUSR, relative
    link = target / "runtime" / "bin" / "python3"
    assert link.is_symlink() and os.readlink(link) == "python3.11"
    assert not (target / "README.md").exists()


@POSIX_ONLY
def test_install_refuses_a_payload_dir_that_is_not_the_placeholder(tmp_path: Path) -> None:
    resources = _placeholder(tmp_path)
    (resources / "payload" / "bin").mkdir()
    with pytest.raises(electron_payload.PayloadInstallError, match="not the empty placeholder"):
        electron_payload.install_payload(resources, _stage(tmp_path), "linux")
    assert (resources / "payload" / "bin").is_dir(), "the refusal must leave the directory alone"


@POSIX_ONLY
def test_install_refuses_a_stage_whose_audio_engine_is_not_executable(tmp_path: Path) -> None:
    resources = _placeholder(tmp_path)
    with pytest.raises(electron_payload.PayloadInstallError, match="bin/odj-audio"):
        electron_payload.install_payload(resources, _stage(tmp_path, audio_executable=False), "linux")
    assert sorted(p.name for p in (resources / "payload").iterdir()) == ["README.md"]


HEAD_SHA = "a" * 40


def _manifest(staged: Path, sha: object) -> None:
    (staged / "manifest.json").write_text(json.dumps({"identity": {"git_sha_full": sha}}), encoding="utf-8")


@POSIX_ONLY
def test_install_takes_a_stage_built_from_this_commit(tmp_path: Path) -> None:
    resources = _placeholder(tmp_path)
    staged = _stage(tmp_path)
    _manifest(staged, HEAD_SHA)
    target = electron_payload.install_payload(resources, staged, "linux", expected_sha=HEAD_SHA)
    assert (target / "manifest.json").is_file()


@POSIX_ONLY
@pytest.mark.parametrize("built", ["b" * 40, None, 7])
def test_install_refuses_a_stale_or_unstamped_stage_and_deletes_nothing(tmp_path: Path, built: object) -> None:
    """A leftover out/payload-stage from another commit must never ship an old engine."""
    resources = _placeholder(tmp_path)
    staged = _stage(tmp_path)
    if built is not None:
        _manifest(staged, built)
    with pytest.raises(electron_payload.PayloadInstallError, match=f"not {HEAD_SHA}"):
        electron_payload.install_payload(resources, staged, "linux", expected_sha=HEAD_SHA)
    assert sorted(p.name for p in (resources / "payload").iterdir()) == ["README.md"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="drives the CLI against the linux-unpacked layout")
@pytest.mark.parametrize("matches_head", [True, False])
def test_the_cli_ties_the_stage_to_the_checkout_head(tmp_path: Path, matches_head: bool) -> None:
    """main() is the documented Linux step: it must pass git's HEAD, so a stage
    from another commit fails the CLI and leaves the placeholder alone."""
    import platform
    import subprocess

    if platform.machine().lower() in ("arm64", "aarch64"):
        pytest.skip("the disposable layout below is linux-unpacked (x64)")
    import shutil

    git = shutil.which("git")
    assert git is not None, "git is not on PATH; this test reads the checkout's HEAD"
    head = subprocess.run(
        [git, "-C", str(electron_payload.REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    resources = _placeholder(tmp_path)
    staged = _stage(tmp_path)
    _manifest(staged, head if matches_head else "b" * 40)
    rc = electron_payload.main(["--staged", str(staged), "--out", str(tmp_path)])
    assert rc == (0 if matches_head else 1)
    listing = sorted(p.name for p in (resources / "payload").iterdir())
    assert (listing == ["README.md"]) is not matches_head, listing


def test_install_refuses_a_stage_without_a_manifest(tmp_path: Path) -> None:
    resources = _placeholder(tmp_path)
    staged = tmp_path / "empty-stage"
    staged.mkdir()
    with pytest.raises(electron_payload.PayloadInstallError, match=r"no manifest\.json"):
        electron_payload.install_payload(resources, staged, "linux")


@POSIX_ONLY
def test_darwin_install_needs_ditto_and_never_falls_back(tmp_path: Path) -> None:
    """A real install run with a PATH that holds no ditto refuses and copies nothing."""
    import subprocess

    resources = _placeholder(tmp_path)
    staged = _stage(tmp_path)
    no_tools = tmp_path / "empty-path"
    no_tools.mkdir()
    code = (
        "import sys; from pathlib import Path; from scripts import electron_payload as e; "
        "e.install_payload(Path(sys.argv[1]), Path(sys.argv[2]), 'darwin')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, str(resources), str(staged)],
        cwd=Path(electron_payload.__file__).resolve().parents[1],
        env={"PATH": str(no_tools)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode != 0
    assert "PayloadInstallError: ditto not found" in out.stderr
    assert not (resources / "payload" / "bin").exists(), "no fallback copy may run without ditto"


def test_windows_needs_a_file_not_an_executable_bit(tmp_path: Path) -> None:
    staged = tmp_path / "p"
    (staged / "bin").mkdir(parents=True)
    for name in ("opendj-engine", "odj-audio"):
        (staged / "bin" / name).write_text("x", encoding="utf-8")
        (staged / "bin" / name).chmod(0o644)
    assert electron_payload.missing_executables(staged, "win32") == []
    if sys.platform != "win32":
        assert electron_payload.missing_executables(staged, "linux") == list(electron_payload.REQUIRED_EXECUTABLES)


@pytest.mark.parametrize(
    ("host", "machine", "expected"),
    [
        ("darwin", "arm64", "mac-arm64/Open DJ.app/Contents/Resources"),
        ("darwin", "x86_64", "mac/Open DJ.app/Contents/Resources"),
        ("linux", "x86_64", "linux-unpacked/resources"),
        ("linux", "aarch64", "linux-arm64-unpacked/resources"),
        ("win32", "AMD64", "win-unpacked/resources"),
    ],
)
def test_packed_resources_dir_matches_electron_builder(host: str, machine: str, expected: str) -> None:
    assert electron_payload.packed_resources_dir(Path("out"), host, machine) == Path("out") / expected


def test_packed_resources_dir_refuses_an_unknown_host() -> None:
    with pytest.raises(electron_payload.PayloadInstallError, match="freebsd"):
        electron_payload.packed_resources_dir(Path("out"), "freebsd14", "amd64")


EXPORT = """--index-url https://pypi.org/simple
--extra-index-url https://download.pytorch.org/whl/cpu
--extra-index-url https://download.pytorch.org/whl/cu126

torch==2.14.0+cpu ; sys_platform == 'linux'
"""


def test_runner_index_args_carry_the_pytorch_index_off_darwin() -> None:
    assert payload_beatgrid.runner_index_args(EXPORT, "linux") == [
        "--extra-index-url",
        "https://download.pytorch.org/whl/cpu",
        "--extra-index-url",
        "https://download.pytorch.org/whl/cu126",
        "--index-strategy",
        "unsafe-best-match",
    ]


def test_runner_index_args_leave_the_darwin_install_unchanged() -> None:
    assert payload_beatgrid.runner_index_args(EXPORT, "darwin") == []


def test_runner_index_args_add_no_strategy_without_an_extra_index() -> None:
    assert payload_beatgrid.runner_index_args("--index-url https://pypi.org/simple\nnumpy==1.26.4\n", "linux") == []


def test_ffmpeg_darwin_line_and_cache_key_are_unchanged() -> None:
    args = build_ffmpeg_lgpl.configure_args(build_ffmpeg_lgpl._PREFIX_PLACEHOLDER, "darwin")
    assert "--arch=arm64" in args and "--cc=clang" in args
    assert not any(arg.startswith("--extra-libs") for arg in args)
    # The name the macOS cache already holds, computed from the pre-change module.
    assert build_ffmpeg_lgpl.artifact_name(args) == "ffmpeg-9.0.2-soxr-0.1.3-lgpl-341981e17a22"


def test_ffmpeg_linux_line_links_libm_and_names_no_macos_flag() -> None:
    args = build_ffmpeg_lgpl.configure_args(Path("/p"), "linux")
    assert "--extra-libs=-lm" in args and "--cc=cc" in args
    assert not any("macosx" in arg or arg == "--arch=arm64" for arg in args)
    assert "--disable-network" in args and not any("gpl" in arg and "enable" in arg and "lgpl" not in arg for arg in args)


@pytest.mark.parametrize(
    ("host", "machine", "refused"),
    [("darwin", "arm64", False), ("darwin", "x86_64", True), ("linux", "x86_64", False), ("win32", "AMD64", True)],
)
def test_ffmpeg_hosts(host: str, machine: str, refused: bool) -> None:
    assert (build_ffmpeg_lgpl.unsupported_host(host, machine) is not None) is refused


def test_only_darwin_measures_mach_o() -> None:
    assert build_engine_payload.measures_mach_o("darwin") is True
    assert build_engine_payload.measures_mach_o("linux") is False


def test_off_darwin_both_library_bars_report_unknown_not_clean() -> None:
    """An empty runtime-load map must not read as "no runtime loads" when the
    classification never ran: both darwin-only bars say UNKNOWN together."""
    fresh = build_engine_payload.PayloadReport()
    # Control: a report nobody marked still claims a measurement.
    assert fresh.link_scan == fresh.runtime_load_scan == "measured"
    build_engine_payload.mark_mach_o_bars_unmeasured(fresh, "linux")
    assert fresh.linked_libraries_scanned is None
    assert fresh.link_scan.startswith("UNKNOWN:") and "linux" in fresh.link_scan
    assert fresh.runtime_load_scan.startswith("UNKNOWN:") and "linux" in fresh.runtime_load_scan


@POSIX_ONLY
def test_install_checks_the_packed_copy_not_only_the_stage(tmp_path: Path) -> None:
    """An entry point that is executable only in the stage must fail the install, not ship a dead launcher.

    bin/odj-audio is a relative symlink that escapes the stage: it resolves to an executable
    from the stage, but the symlink-keeping copy leaves it dangling in the packed tree.
    """
    outside = tmp_path / "odj-audio-outside-stage"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    outside.chmod(0o755)
    staged = _stage(tmp_path)
    audio = staged / "bin" / "odj-audio"
    audio.unlink()
    audio.symlink_to(Path("..", "..", outside.name))
    assert electron_payload.missing_executables(staged, "linux") == [], "the stage itself must pass"
    with pytest.raises(electron_payload.PayloadInstallError, match=r"the copy in .* lost executable bin/odj-audio"):
        electron_payload.install_payload(_placeholder(tmp_path), staged, "linux")


def _launcher_with_echo_runtime(tmp_path: Path) -> Path:
    payload = tmp_path / "payload"
    runtime = payload / "runtime" / "bin"
    runtime.mkdir(parents=True)
    fake = runtime / "python3"
    fake.write_text('#!/bin/sh\nprintf "%s" "${MDT_LIBRARY_MODE-UNSET}"\n', encoding="utf-8")
    fake.chmod(0o755)
    return build_engine_payload.write_engine_launcher(payload)


@POSIX_ONLY
@pytest.mark.parametrize(("given", "expected"), [(None, "local"), ("remote", "remote")])
def test_engine_launcher_states_local_unless_the_caller_chose(tmp_path: Path, given: str | None, expected: str) -> None:
    """Linux refuses an unset library mode (apps/shared/library_mode.py); a caller's choice still wins."""
    import subprocess

    env = {"PATH": "/usr/bin:/bin"}
    if given is not None:
        env["MDT_LIBRARY_MODE"] = given
    out = subprocess.run(["/bin/sh", str(_launcher_with_echo_runtime(tmp_path))], env=env, capture_output=True, text=True, check=True)
    assert out.stdout == expected


def test_payload_verify_env_matches_the_launcher_default(tmp_path: Path) -> None:
    assert build_engine_payload._payload_interpreter_env(str(tmp_path), tmp_path)["MDT_LIBRARY_MODE"] == "local"


@pytest.mark.parametrize(
    ("installed", "pinned", "ok"),
    [
        ("2.14.0", "2.14.0", True),
        ("2.14.0+cpu", "2.14.0", True),
        ("2.14.1+cpu", "2.14.0", False),
        ("2.14.0", "2.14.0+cpu", False),
        ("2.14.0+cu126", "2.14.0+cpu", False),
        ("2.14.01", "2.14.0", False),
        (None, "2.14.0", False),
    ],
)
def test_runner_pin_accepts_a_local_label_only_on_the_same_public_version(installed: str | None, pinned: str, ok: bool) -> None:
    assert payload_beatgrid.pin_satisfied(installed, pinned) is ok
