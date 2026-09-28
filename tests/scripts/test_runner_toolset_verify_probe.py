"""scripts/runner_toolset_verify.py's probe script, run for real under bash.

Split out of tests/scripts/test_runner_toolset_verify.py (classification and
the exit-code contract, from the probe's record format). Here the probe runs
against the real executables installed where the tests run and real runner
directories built in tmp_path. No stand-ins: a missing toolchain is reported
UNAVAILABLE and skipped.

Regression lines:
  - if cargo and rustc share one entry, or one's pin decides the other's verdict,
    then broken
  - if a probe with no readable runner .path verifies against the login PATH then broken
  - if a rustup with no default fails the Rust probe tests instead of pinning an
    installed toolchain (or skipping UNAVAILABLE when none is installed) then broken
  - if the Rust probe tests skip or pin when the rustup default already runs then broken
  - if the verifier reports anything but MISSING for a rustup with no default then broken
  - if a host with several runner dirs is verified against one runner's .path
    only, or the unsuffixed `actions-runner` dir is skipped, then broken
  - if runner dirs sharing one .path are probed more than once, or a dir with
    no `.runner` (not a configured runner) makes the run UNKNOWN, then broken
  - if a configured runner dir with neither a `.env` PATH= nor a readable .path
    does not make every entry UNKNOWN then broken
  - if a runner's stale .path is verified when its `.env` PATH= (which the
    Listener loads and which wins) is set, or runners are deduplicated on .path
    rather than on that effective job PATH, then broken
  - if a Playwright revision dir left by an interrupted install (no
    INSTALLATION_COMPLETE) or missing its executable reports OK then broken
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import runner_toolset_verify as rtv
from scripts.runner_toolset_scan import load_manifest


def _entry(
    name: str, version: str, kind: str = "binary", provides: list[str] | None = None
) -> dict:
    return {
        "name": name,
        "kind": kind,
        "version": version,
        "match": "exact",
        "verify": "true",
        "provides": provides or [],
    }


# ----- the probe itself, run for real against the installed executables -------------

PROBE_NEEDS = ("bash", "timeout", "base64", "head", "cat")


def _dirs_of(*tools: str) -> list[str]:
    """Real directories holding `tools`; a probe prerequisite missing here fails loud."""
    found = {tool: shutil.which(tool) for tool in tools}
    missing = sorted(tool for tool, path in found.items() if not path)
    assert not missing, f"probe prerequisites absent on this host: {missing}"
    return sorted({str(Path(path).parent) for path in found.values() if path})


def _probe_locally(
    entries: list[dict],
    runner_dir: Path | None,
    login_path: str | None = None,
    env: dict[str, str] | None = None,
    runner_dir_globs: str = rtv.CFG_RUNNER_DIR_GLOBS,
) -> tuple[list, str]:
    """Run the verifier's own probe script under bash, as this user, no sudo.

    HOME stays this user's real one: the verifier's `sudo -H` gives the probe the
    runner user's real HOME, and rustup reads its default toolchain from there."""
    proc = subprocess.run(
        ["bash", "-s"],
        input=rtv._probe_script(entries, str(runner_dir) if runner_dir else None, runner_dir_globs),
        capture_output=True,
        text=True,
        timeout=120,
        env={**(env or _passthrough_env()), "PATH": login_path or os.environ["PATH"]},
        check=False,
    )
    return rtv.classify(entries, proc.returncode, proc.stdout, proc.stderr), proc.stdout


def _runner_with_path(tmp_path: Path, dirs: list[str]) -> Path:
    runner = tmp_path / "actions-runner-1"
    runner.mkdir()
    (runner / ".path").write_text(":".join(dirs), encoding="utf-8")
    return runner


def _rust_entries() -> tuple[str, str, list[dict]]:
    """(cargo entry name, rustc entry name, both entries) from the live manifest."""
    entries = load_manifest()["entries"]
    by_exe = {exe: e for e in entries for exe in e.get("provides", [])}
    cargo, rustc = by_exe["cargo"], by_exe["rustc"]
    assert cargo["name"] != rustc["name"], (
        f"cargo and rustc share entry {cargo['name']!r}: one version pin cannot vouch "
        "for two executables' output"
    )
    return cargo["name"], rustc["name"], [cargo, rustc]


# ----- a rustup with no default: pin an installed toolchain, or skip UNAVAILABLE ------------

RUST_ENV_KEYS = ("HOME", "RUSTUP_HOME", "CARGO_HOME", "RUSTUP_TOOLCHAIN")
RUST_TOOLS = ("cargo", "rustc")
TOOLCHAIN_LINE_RE = re.compile(r"^([A-Za-z0-9][\w.+-]*)(?: \(.*\))?$")


@dataclass(frozen=True)
class RustChoice:
    """What the Rust probe tests do: run as-is, pin RUSTUP_TOOLCHAIN, or skip."""

    pin: str | None
    skip: str | None


def _passthrough_env() -> dict[str, str]:
    """PATH plus the real HOME and rustup/cargo state this user's tools read."""
    kept = {key: os.environ[key] for key in RUST_ENV_KEYS if key in os.environ}
    return {"PATH": os.environ["PATH"], **kept}


def _installed_toolchains(listing: str) -> list[str]:
    """Toolchain names from `rustup toolchain list`, which prints one per line
    (suffixed `(default)` or `(active, default)`), or `no installed toolchains`."""
    lines = (TOOLCHAIN_LINE_RE.match(line.strip()) for line in listing.splitlines())
    return [line.group(1) for line in lines if line]


def _choose_rust_toolchain(default_runs: bool, listing: str | None) -> RustChoice:
    """Never a failure: a working default runs as-is, a rustup with no default is
    pinned to an installed toolchain, and only no toolchain at all is UNAVAILABLE.
    `listing` is None when no rustup is installed."""
    if default_runs:
        return RustChoice(pin=None, skip=None)
    if listing is None:
        return RustChoice(pin=None, skip="UNAVAILABLE: cargo/rustc do not run and no rustup")
    toolchains = _installed_toolchains(listing)
    if not toolchains:
        return RustChoice(pin=None, skip="UNAVAILABLE: rustup has no installed toolchain")
    return RustChoice(pin=toolchains[0], skip=None)


def _all_rust_tools_run(env: dict[str, str]) -> bool:
    """Availability is running the tool to exit 0, not finding it on PATH: a rustup
    proxy with no default is on PATH and cannot run."""
    for tool in RUST_TOOLS:
        try:
            proc = subprocess.run(
                [tool, "--version"], env=env, capture_output=True, timeout=60, check=False
            )
        except FileNotFoundError:
            return False
        if proc.returncode != 0:
            return False
    return True


def _rustup_listing(env: dict[str, str]) -> str | None:
    try:
        proc = subprocess.run(
            ["rustup", "toolchain", "list"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        return None
    assert proc.returncode == 0, f"`rustup toolchain list` failed: {proc.stderr}"
    return proc.stdout


def _rust_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """An env where the real cargo and rustc both run, or pytest.skip UNAVAILABLE."""
    env = base or _passthrough_env()
    default_runs = _all_rust_tools_run(env)
    choice = _choose_rust_toolchain(default_runs, None if default_runs else _rustup_listing(env))
    if choice.skip:
        pytest.skip(choice.skip)
    pinned = {**env, "RUSTUP_TOOLCHAIN": choice.pin} if choice.pin else env
    assert _all_rust_tools_run(pinned), f"cargo/rustc do not run with RUSTUP_TOOLCHAIN={choice.pin}"
    return pinned


def _installed_version(tool: str, env: dict[str, str]) -> str:
    """The real toolchain's own reported version."""
    out = subprocess.run(
        [tool, "--version"], env=env, capture_output=True, text=True, check=True
    ).stdout
    found = re.match(rf"{tool} (\d+\.\d+\.\d+)\b", out)
    assert found, f"unexpected `{tool} --version` output: {out!r}"
    return found.group(1)


LISTED = "1.98.1-x86_64-unknown-linux-gnu\nnightly-x86_64-unknown-linux-gnu\n"


@pytest.mark.parametrize(
    ("default_runs", "listing", "expected"),
    [
        (True, None, RustChoice(pin=None, skip=None)),
        (True, "stable-aarch64-apple-darwin (default)\n", RustChoice(pin=None, skip=None)),
        (True, LISTED, RustChoice(pin=None, skip=None)),
        (False, LISTED, RustChoice(pin="1.98.1-x86_64-unknown-linux-gnu", skip=None)),
        (
            False,
            "stable-x86_64-unknown-linux-gnu (active, default)\n",
            RustChoice(pin="stable-x86_64-unknown-linux-gnu", skip=None),
        ),
        (
            False,
            "no installed toolchains\n",
            RustChoice(pin=None, skip="UNAVAILABLE: rustup has no installed toolchain"),
        ),
        (False, "", RustChoice(pin=None, skip="UNAVAILABLE: rustup has no installed toolchain")),
        (
            False,
            None,
            RustChoice(pin=None, skip="UNAVAILABLE: cargo/rustc do not run and no rustup"),
        ),
    ],
)
def test_a_rustup_with_no_default_pins_or_skips_and_a_default_runs_as_is(
    default_runs: bool, listing: str | None, expected: RustChoice
) -> None:
    """The choice the Rust probe tests make, over real `rustup toolchain list`
    output shapes. No default is never a failure; a working default is never
    skipped or re-pinned (the overshoot)."""
    assert _choose_rust_toolchain(default_runs, listing) == expected


def _rustup_home_without_default(tmp_path: Path, link_toolchains: bool) -> dict[str, str]:
    """An env whose RUSTUP_HOME is a fresh dir with no settings.toml, so rustup has
    no default; its toolchains dir links the real installed ones when asked."""
    env = _rust_env()
    try:
        real_home = subprocess.run(
            ["rustup", "show", "home"], env=env, capture_output=True, text=True, check=True
        ).stdout.strip()
    except FileNotFoundError:
        pytest.skip("UNAVAILABLE: no rustup here, so a rustup with no default cannot be built")
    rustup_home = tmp_path / "rustup-home"
    rustup_home.mkdir()
    if link_toolchains:
        (rustup_home / "toolchains").symlink_to(Path(real_home) / "toolchains")
    bare = {key: value for key, value in env.items() if key != "RUSTUP_TOOLCHAIN"}
    return {**bare, "RUSTUP_HOME": str(rustup_home)}


def test_the_verifier_reports_missing_for_a_rustup_with_no_default(tmp_path: Path) -> None:
    """The host state behind the runner failure: rustup proxies on PATH, toolchains
    installed, no default. cargo/rustc cannot run there, so the verifier is right
    to say MISSING; the tests pin RUSTUP_TOOLCHAIN and the same probe is OK."""
    no_default = _rustup_home_without_default(tmp_path, link_toolchains=True)
    assert not _all_rust_tools_run(no_default), "the no-default state still runs cargo/rustc"
    cargo, rustc, entries = _rust_entries()
    pinned_env = _rust_env(no_default)
    assert pinned_env.get("RUSTUP_TOOLCHAIN"), pinned_env
    versions = {tool: _installed_version(tool, pinned_env) for tool in RUST_TOOLS}
    pinned = [
        {**entries[0], "version": versions["cargo"]},
        {**entries[1], "version": versions["rustc"]},
    ]
    runner = _runner_with_path(tmp_path, _dirs_of(*PROBE_NEEDS, *RUST_TOOLS))
    missing, _ = _probe_locally(pinned, runner, env=no_default)
    assert {r.name: r.status for r in missing} == {cargo: "MISSING", rustc: "MISSING"}, missing
    ok, _ = _probe_locally(pinned, runner, env=pinned_env)
    assert {r.name: r.status for r in ok} == {cargo: "OK", rustc: "OK"}, ok


def test_a_rustup_with_no_toolchain_skips_unavailable(tmp_path: Path) -> None:
    """With nothing installed there is nothing to pin: a named skip, not a failure."""
    empty = _rustup_home_without_default(tmp_path, link_toolchains=False)
    with pytest.raises(pytest.skip.Exception, match=r"^UNAVAILABLE: rustup has no installed"):
        _rust_env(empty)


@pytest.mark.parametrize(
    ("moved", "expected"),
    [
        (None, {"cargo": "OK", "rustc": "OK"}),
        ("rustc", {"cargo": "OK", "rustc": "MISMATCH"}),
        ("cargo", {"cargo": "MISMATCH", "rustc": "OK"}),
    ],
)
def test_each_rust_executable_is_verified_against_its_own_pin(
    tmp_path: Path, moved: str | None, expected: dict[str, str]
) -> None:
    """The manifest's own Rust entries, probed against the real installed toolchain.

    Pinning both at the installed versions is OK (the overshoot control). Moving
    one entry's pin off its installed version flips only that entry: a pinned
    cargo cannot vouch for rustc, or the reverse. Each verdict also quotes its own
    executable's output, which a combined `cargo --version && rustc --version`
    would not."""
    env = _rust_env()
    cargo, rustc, entries = _rust_entries()
    installed = {tool: _installed_version(tool, env) for tool in RUST_TOOLS}
    pins = {tool: "0.0.1" if tool == moved else version for tool, version in installed.items()}
    pinned = [{**entries[0], "version": pins["cargo"]}, {**entries[1], "version": pins["rustc"]}]
    runner = _runner_with_path(tmp_path, _dirs_of(*PROBE_NEEDS, *RUST_TOOLS))
    results, _ = _probe_locally(pinned, runner, env=env)
    by_name = {r.name: r for r in results}
    assert {"cargo": by_name[cargo].status, "rustc": by_name[rustc].status} == expected, results
    assert by_name[cargo].got.startswith("cargo "), by_name[cargo]
    assert by_name[rustc].got.startswith("rustc "), by_name[rustc]
    assert rtv.exit_code(results) == (0 if moved is None else 1)


# ----- the job PATH is a prerequisite, never replaced by the login PATH ------------------


def _bash_entry(job_dirs: list[str] | None = None) -> dict:
    """An entry pinned to the bash that `job_dirs` (or the login PATH) resolves."""
    bash = shutil.which("bash", path=":".join(job_dirs) if job_dirs else None)
    assert bash, f"no bash on {job_dirs or 'the login PATH'}"
    out = subprocess.run([bash, "--version"], capture_output=True, text=True, check=True).stdout
    version = re.search(r"version (\d+\.\d+\.\d+)", out)
    assert version, out
    entry = _entry("bash", version.group(1), kind="binary", provides=["bash"])
    entry["verify"] = "bash --version | head -1"
    return entry


@pytest.mark.parametrize("setup", ["no-runner-dir", "no-path-file", "empty-path-file"])
def test_no_readable_runner_path_is_unknown_for_every_entry(tmp_path: Path, setup: str) -> None:
    """Without the runner's .path the probe cannot know what a job resolves, so it
    must not fall back to the invoking shell's PATH and vouch for anything."""
    runner = tmp_path / "actions-runner-1"
    if setup != "no-runner-dir":
        runner.mkdir()
    if setup == "empty-path-file":
        (runner / ".path").write_text("", encoding="utf-8")
    results, stdout = _probe_locally([_bash_entry()], runner)
    assert {r.status for r in results} == {"UNKNOWN"}, results
    assert ".path" in results[0].got, results
    assert rtv.exit_code(results) == 2
    assert f"{rtv.RECORD} END" not in stdout, stdout


def _only_on_login_path(job_dirs: list[str]) -> tuple[str, str]:
    """(name, dir) of a real executable in this interpreter's bin dir that no job
    PATH directory holds."""
    bindir = Path(sys.executable).parent
    for exe in sorted(bindir.iterdir()):
        on_job_path = any((Path(d) / exe.name).exists() for d in job_dirs)
        if exe.is_file() and os.access(exe, os.X_OK) and not on_job_path:
            return exe.name, str(bindir)
    raise AssertionError(f"no executable in {bindir} is absent from the job PATH {job_dirs}")


def test_a_readable_runner_path_is_the_path_every_check_uses(tmp_path: Path) -> None:
    """Positive control, with real executables: a readable .path is used, a tool on
    it is OK, and a tool present only on the invoking shell's PATH is MISSING."""
    job_dirs = _dirs_of(*PROBE_NEEDS)
    runner = _runner_with_path(tmp_path, job_dirs)
    outside, outside_dir = _only_on_login_path(job_dirs)
    elsewhere = _entry("elsewhere", "1", kind="binary", provides=[outside])
    login_path = f"{outside_dir}:{os.environ['PATH']}"
    entries = [_bash_entry(job_dirs), elsewhere]
    results, stdout = _probe_locally(entries, runner, login_path)
    assert f"{rtv.RECORD} PATHSRC {runner}" in stdout, stdout
    status = {r.name: (r.status, r.got) for r in results}
    assert status["bash"][0] == "OK", status
    assert status["elsewhere"][0] == "MISSING" and "PATH" in status["elsewhere"][1], status


# ----- every configured runner on the host, not the first one found ---------------------


def _runner_dir(
    root: Path,
    name: str,
    path_dirs: list[str] | None,
    configured: bool = True,
    env_path_dirs: list[str] | None = None,
) -> Path:
    """A runner install dir: `.runner` marks it configured; its job PATH is `.env`'s
    `PATH=` when present (the Listener loads it and it wins), else `.path`."""
    runner = root / name
    runner.mkdir()
    if configured:
        (runner / ".runner").write_text("{}", encoding="utf-8")
    if path_dirs is not None:
        (runner / ".path").write_text(":".join(path_dirs) + "\n", encoding="utf-8")
    if env_path_dirs is not None:
        env = f"LANG=en_US.UTF-8\nPATH={':'.join(env_path_dirs)}\nRUSTUP_HOME=/nonexistent\n"
        (runner / ".env").write_text(env, encoding="utf-8")
    return runner


def _globs_under(root: Path) -> str:
    """The verifier's default runner-dir globs rebased onto `root`, same shape."""
    globs = rtv.CFG_RUNNER_DIR_GLOBS.replace("/opt/", f"{root}/").replace("/home/*/", f"{root}/")
    assert globs != rtv.CFG_RUNNER_DIR_GLOBS, rtv.CFG_RUNNER_DIR_GLOBS
    return globs


def _elsewhere_entry(job_dirs: list[str]) -> tuple[dict, str]:
    """An entry whose provided executable sits only in a dir no job PATH holds."""
    outside, outside_dir = _only_on_login_path(job_dirs)
    entry = _entry("elsewhere", "1", kind="binary", provides=[outside])
    entry["verify"] = "echo elsewhere 1"
    return entry, outside_dir


@pytest.mark.parametrize(
    ("lacking", "complete"),
    [("actions-runner", "actions-runner-2"), ("actions-runner-3", "actions-runner-2")],
)
def test_every_configured_runner_path_is_verified(
    tmp_path: Path, lacking: str, complete: str
) -> None:
    """A tool on one runner's job PATH says nothing about another's: the host is
    MISSING when any configured runner, suffixed or not, cannot resolve it."""
    job_dirs = _dirs_of(*PROBE_NEEDS)
    entry, outside_dir = _elsewhere_entry(job_dirs)
    bad = _runner_dir(tmp_path, lacking, job_dirs)
    _runner_dir(tmp_path, complete, [outside_dir, *job_dirs])
    results, stdout = _probe_locally([entry], None, runner_dir_globs=_globs_under(tmp_path))
    [result] = results
    assert result.status == "MISSING" and str(bad) in result.got, (result, stdout)
    assert rtv.exit_code(results) == 1


def test_runners_sharing_a_path_are_probed_once_and_unconfigured_dirs_are_ignored(
    tmp_path: Path,
) -> None:
    """Overshoot control: identical job PATHs give identical verdicts, so one probe
    covers them, and a dir with no `.runner` is not a runner to verify."""
    job_dirs = _dirs_of(*PROBE_NEEDS)
    entry, outside_dir = _elsewhere_entry(job_dirs)
    first = _runner_dir(tmp_path, "actions-runner", [outside_dir, *job_dirs])
    second = _runner_dir(tmp_path, "actions-runner-2", [outside_dir, *job_dirs])
    _runner_dir(tmp_path, "actions-runner-cache", None, configured=False)
    results, stdout = _probe_locally(
        [_bash_entry(job_dirs), entry], None, runner_dir_globs=_globs_under(tmp_path)
    )
    assert {r.name: r.status for r in results} == {"bash": "OK", "elsewhere": "OK"}, results
    assert stdout.count(f"{rtv.RECORD} PATHSRC ") == 1, stdout
    assert f"{rtv.RECORD} PATHSRC {first}" in stdout, stdout
    assert f"{rtv.RECORD} ALSO {second} {first}" in stdout, stdout


def test_a_configured_runner_without_a_path_makes_the_host_unknown(
    tmp_path: Path,
) -> None:
    """One runner whose job PATH cannot be read cannot be vouched for, so the whole
    run refuses (UNKNOWN) rather than verifying only the readable ones."""
    job_dirs = _dirs_of(*PROBE_NEEDS)
    _runner_dir(tmp_path, "actions-runner", job_dirs)
    unreadable = _runner_dir(tmp_path, "actions-runner-2", None)
    results, stdout = _probe_locally(
        [_bash_entry(job_dirs)], None, runner_dir_globs=_globs_under(tmp_path)
    )
    assert {r.status for r in results} == {"UNKNOWN"}, results
    assert str(unreadable) in results[0].got, results
    assert f"{rtv.RECORD} END" not in stdout, stdout


@pytest.mark.parametrize(
    ("path_file", "env_path", "status"),
    [("stale", "complete", "OK"), ("complete", "stale", "MISSING"), (None, "complete", "OK")],
)
def test_the_env_path_is_the_job_path_and_wins_over_the_path_file(
    tmp_path: Path, path_file: str | None, env_path: str, status: str
) -> None:
    """The runner Listener loads `.env`, and a PATH= there wins over `.path`, which
    runsvc.sh only exports as a fallback. A stale `.path` under a correct `.env` is
    inert (OK); a correct `.path` under a stale `.env` PATH is not (MISSING)."""
    job_dirs = _dirs_of(*PROBE_NEEDS)
    entry, outside_dir = _elsewhere_entry(job_dirs)
    dirs = {"stale": job_dirs, "complete": [outside_dir, *job_dirs]}
    runner = _runner_dir(
        tmp_path,
        "actions-runner",
        dirs[path_file] if path_file else None,
        env_path_dirs=dirs[env_path],
    )
    [result], _ = _probe_locally([entry], runner)
    assert result.status == status, result


def test_runners_are_deduplicated_on_their_effective_job_path(
    tmp_path: Path,
) -> None:
    """Two runners whose `.path` files differ but whose `.env` PATH is the same run
    jobs under one PATH, so they are one probe, not two."""
    job_dirs = _dirs_of(*PROBE_NEEDS)
    entry, outside_dir = _elsewhere_entry(job_dirs)
    effective = [outside_dir, *job_dirs]
    first = _runner_dir(tmp_path, "actions-runner", job_dirs, env_path_dirs=effective)
    stale = [*job_dirs, "/nowhere"]
    second = _runner_dir(tmp_path, "actions-runner-2", stale, env_path_dirs=effective)
    results, stdout = _probe_locally([entry], None, runner_dir_globs=_globs_under(tmp_path))
    assert [r.status for r in results] == ["OK"], results
    assert stdout.count(f"{rtv.RECORD} PATHSRC ") == 1, stdout
    assert f"{rtv.RECORD} ALSO {second} {first}" in stdout, stdout


# ----- a Playwright browser is its payload, not its cache dir name ------------------------

PLAYWRIGHT_DIR_RE = re.compile(r"ms-playwright/([\w-]+-\d+)")


def _playwright_entries() -> list[tuple[dict, str]]:
    """(entry, revision dir name) for every Playwright entry in the live manifest."""
    entries = [e for e in load_manifest()["entries"] if e["kind"] == "playwright"]
    assert entries, "no playwright entries in the manifest: the test checks nothing"
    named = [(e, PLAYWRIGHT_DIR_RE.search(e["verify"])) for e in entries]
    unnamed = [e["name"] for e, found in named if not found]
    assert not unnamed, f"verify names no ms-playwright revision dir: {unnamed}"
    return [(e, found.group(1)) for e, found in named if found]


@pytest.mark.parametrize("state", ["interrupted-install", "payload-deleted"])
def test_a_playwright_revision_dir_without_its_payload_is_missing(
    tmp_path: Path, state: str
) -> None:
    """An interrupted install leaves the revision dir with no INSTALLATION_COMPLETE
    marker; a later cleanup can leave the marker and delete the browser. Neither
    can launch, so neither may verify OK on the dir name alone."""
    home = tmp_path / "home"
    entries = _playwright_entries()
    for _, dirname in entries:
        revision = home / ".cache" / "ms-playwright" / dirname
        revision.mkdir(parents=True)
        if state == "payload-deleted":
            (revision / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")
            (revision / "DEPENDENCIES_VALIDATED").write_text("", encoding="utf-8")
    runner = _runner_with_path(tmp_path, _dirs_of(*PROBE_NEEDS, "ls", "test"))
    results, _ = _probe_locally([e for e, _ in entries], runner, env={"HOME": str(home)})
    assert {r.name: r.status for r in results} == {e["name"]: "MISSING" for e, _ in entries}, (
        results
    )


def test_a_complete_playwright_install_verifies_ok_and_its_marker_is_what_counts(
    tmp_path: Path,
) -> None:
    """On the real cache: a revision Playwright itself marked INSTALLATION_COMPLETE
    verifies OK (the overshoot control), and the same real payload without that
    marker, as an install interrupted after unpacking leaves it, is MISSING."""
    cache = Path.home() / ".cache" / "ms-playwright"
    entries = _playwright_entries()
    absent = [d for _, d in entries if not (cache / d / "INSTALLATION_COMPLETE").is_file()]
    if absent:
        pytest.skip(f"UNAVAILABLE: no completed Playwright install here for {absent}")
    runner = _runner_with_path(tmp_path, _dirs_of(*PROBE_NEEDS, "ls", "test"))
    results, _ = _probe_locally([e for e, _ in entries], runner)
    assert {r.status for r in results} == {"OK"}, results
    home = tmp_path / "home"
    for _, dirname in entries:
        unmarked = home / ".cache" / "ms-playwright" / dirname
        unmarked.mkdir(parents=True)
        for part in (cache / dirname).iterdir():
            if part.name != "INSTALLATION_COMPLETE":
                (unmarked / part.name).symlink_to(part)
    results, _ = _probe_locally([e for e, _ in entries], runner, env={"HOME": str(home)})
    assert {r.status for r in results} == {"MISSING"}, results
