"""scripts/runner_toolset_verify.py classifies a host truthfully.

A verdict about a real runner host comes only from running the probe against
that host (by hand, and from the host audit, against agentbox). These tests pin
the classification and exit-code contract the host audit depends on, using the
probe's own record format, and run the probe script itself under bash against
the real executables installed where the tests run (no stand-ins: a missing
toolchain is reported UNAVAILABLE and skipped).

Regression lines:
  - if a verify that prints another version reports OK then broken
  - if a verify that exits nonzero reports anything but MISSING then broken
  - if a provided executable absent from the runner PATH reports OK then broken
  - if a probe that never ran (ssh or sudo failure) reports OK or MISSING then broken
  - if one UNKNOWN among OKs exits 0, or one MISSING exits other than 1, then broken
  - if an apt package on a NEWER Ubuntu revision than the pin reports MISMATCH then broken
    (it would push hosts to downgrade security fixes)
  - if an apt package on an OLDER revision than the pin reports OK then broken
  - if a `provision: job` entry absent from the host fails the run then broken
  - if a pin followed by a prerelease or build suffix reports OK then broken
  - if cargo and rustc share one entry, or one's pin decides the other's verdict,
    then broken
  - if a probe with no readable runner .path verifies against the login PATH then broken
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import runner_toolset_verify as rtv
from scripts.runner_toolset_scan import load_manifest


def _entry(
    name: str,
    version: str,
    kind: str = "apt",
    match: str = "exact",
    provides: list[str] | None = None,
) -> dict:
    return {
        "name": name,
        "kind": kind,
        "version": version,
        "match": match,
        "verify": "true",
        "provides": provides or [],
    }


def _record(name: str, rc: int, output: str) -> str:
    return f"{rtv.RECORD} V {name} {rc} {base64.b64encode(output.encode()).decode()}"


def _classify(
    entries: list[dict], records: list[str], rc: int = 0, stderr: str = ""
) -> list[rtv.Result]:
    return rtv.classify(entries, rc, "\n".join([*records, f"{rtv.RECORD} END"]), stderr)


@pytest.mark.parametrize(
    ("version", "match", "output", "status"),
    [
        ("6.0-28ubuntu4.1", "exact", "ii 6.0-28ubuntu4.1", "OK"),
        ("6.0-28ubuntu4.1", "exact", "ii 6.0-28ubuntu4.2", "MISMATCH"),
        ("3.11.16", "exact", "3.11.160", "MISMATCH"),
        ("22.14.0", "exact", "v22.14.0", "OK"),
        ("3.11", "prefix", "3.11.16", "OK"),
        ("3.11", "prefix", "3.12.3", "MISMATCH"),
        ("0.12.19", "min", "uv 0.13.0 (x86_64)", "OK"),
        ("0.12.19", "min", "uv 0.12.9 (x86_64)", "MISMATCH"),
    ],
)
def test_version_comparison(version: str, match: str, output: str, status: str) -> None:
    [result] = _classify([_entry("t", version, match=match)], [_record("t", 0, output)])
    assert result.status == status, result


def test_nonzero_verify_is_missing_not_unknown() -> None:
    [result] = _classify([_entry("unzip", "6.0")], [_record("unzip", 1, "")])
    assert result.status == "MISSING", result


def test_timed_out_verify_is_unknown() -> None:
    [result] = _classify([_entry("slow", "1.0")], [_record("slow", 124, "")])
    assert result.status == "UNKNOWN", result


def test_provided_executable_off_path_is_missing() -> None:
    entries = [_entry("gh", "2.100.0", kind="binary", provides=["gh"])]
    records = [_record("gh", 0, "gh version 2.100.0"), f"{rtv.RECORD} P gh gh"]
    [result] = _classify(entries, records)
    assert result.status == "MISSING" and "PATH" in result.got, result


def test_probe_that_never_ran_is_unknown_for_every_entry() -> None:
    entries = [_entry("a", "1"), _entry("b", "2")]
    results = rtv.classify(entries, 255, "", "ssh: Could not resolve hostname nohost")
    assert {r.status for r in results} == {"UNKNOWN"}
    assert "Could not resolve" in results[0].got


def test_truncated_probe_is_unknown_even_with_records() -> None:
    """A probe cut off before END cannot vouch for anything, OK records included."""
    results = rtv.classify([_entry("a", "1")], 0, _record("a", 0, "1"), "")
    assert results[0].status == "UNKNOWN"


@pytest.mark.parametrize(
    ("statuses", "code"),
    [
        (["OK", "OK"], 0),
        (["OK", "UNKNOWN"], 2),
        (["OK", "MISSING"], 1),
        (["MISMATCH", "UNKNOWN"], 1),
        ([], 2),
    ],
)
def test_exit_code_contract(statuses: list[str], code: int) -> None:
    results = [rtv.Result(f"e{i}", "apt", s, "1", "1") for i, s in enumerate(statuses)]
    assert rtv.exit_code(results) == code


def test_probe_script_runs_every_verify_and_checks_path_kinds_only() -> None:
    entries = [
        _entry("unzip", "6.0", provides=["unzip"]),
        _entry("toolcache-uv", "0.12", kind="toolcache", provides=["uv"]),
    ]
    script = rtv._probe_script(entries, "/opt/actions-runner-10")
    assert script.count(" V ") == 2
    assert "command -v unzip" in script and "command -v uv" not in script
    assert script.rstrip().endswith(f'echo "{rtv.RECORD} END"')


# ----- apt floors: newer security revisions pass, older ones do not ------------------


@pytest.mark.parametrize(
    ("pinned", "installed", "status"),
    [
        ("2:4.0.4-4ubuntu3.2", "2:4.0.4-4ubuntu3.3", "OK"),  # procps on agbox2/3
        ("0.9.0-1ubuntu0.1", "0.9.0-1ubuntu0.3", "OK"),  # bubblewrap on agbox2/3
        ("3.4.4-5ubuntu0.7", "3.4.4-5ubuntu0.8", "OK"),  # libsoup-3.0-dev on agbox2/3
        ("2:4.0.4-4ubuntu3.2", "2:4.0.4-4ubuntu3.2", "OK"),
        ("2:4.0.4-4ubuntu3.2", "2:4.0.4-4ubuntu3.1", "MISMATCH"),
        ("0.9.0-1ubuntu0.1", "0.9.0-1", "MISMATCH"),
        ("1:3.10-1ubuntu0.1", "3.10-1ubuntu0.1", "MISMATCH"),  # the epoch outranks everything
    ],
)
def test_apt_pins_are_floors_under_dpkg_ordering(pinned: str, installed: str, status: str) -> None:
    entry = {**_entry("pkg", pinned), "match": None}
    del entry["match"]  # the apt default, not an explicit mode
    [result] = _classify([entry], [_record("pkg", 0, f"ii {installed}")])
    assert result.status == status, result


def test_an_explicit_exact_pin_on_apt_still_rejects_a_newer_revision() -> None:
    entry = _entry("pkg", "2:4.0.4-4ubuntu3.2", match="exact")
    [result] = _classify([entry], [_record("pkg", 0, "ii 2:4.0.4-4ubuntu3.3")])
    assert result.status == "MISMATCH", result


@pytest.mark.parametrize(
    ("a", "b", "order"),
    [("1.0~rc1", "1.0", -1), ("1.0+b1", "1.0", 1), ("1.10", "1.9", 1), ("1:0.1", "2.0", 1)],
)
def test_dpkg_compare_agrees_with_dpkg(a: str, b: str, order: int) -> None:
    """Expected values checked with `dpkg --compare-versions` on agentbox, Mon 28 Sep 2026."""
    assert rtv.dpkg_compare(a, b) == order
    assert rtv.dpkg_compare(b, a) == -order


def test_job_provisioned_entry_is_info_and_does_not_fail_the_run() -> None:
    entry = {**_entry("cargo-audit", "0.22.2", kind="binary"), "provision": "job"}
    records = [_record("cargo-audit", 101, "error: no such command: `audit`")]
    [result] = _classify([entry], records)
    assert result.status == "INFO", result
    assert rtv.exit_code([result]) == 0


# ----- exact and prefix pins name the WHOLE version, suffix included ------------------


@pytest.mark.parametrize(
    ("version", "match", "output"),
    [
        ("1.96.0", "exact", "rustc 1.96.0-nightly (17067e9ac 2026-08-01)"),
        ("22.23.2", "exact", "v22.23.2-rc.1"),
        ("1.96.0", "exact", "cargo 1.96.0+b1"),
        ("1.58.0", "exact", "just 1.58.0beta"),
        ("0.34.6", "exact", "0.34.6~rc1"),
        ("3.11", "prefix", "3.11.16rc1"),
        ("3.11", "prefix", "3.11.16+local"),
    ],
)
def test_a_suffixed_version_is_a_mismatch(version: str, match: str, output: str) -> None:
    """A prerelease or build suffix is a different toolchain, not the pinned one."""
    [result] = _classify([_entry("t", version, match=match)], [_record("t", 0, output)])
    assert result.status == "MISMATCH", result


@pytest.mark.parametrize(
    ("version", "output"),
    [
        ("1.96.0", "rustc 1.96.0 (17067e9ac 2026-08-01)"),
        ("1.96.0", "cargo 1.96.0 (5ffbef321 2026-07-20)"),
        ("22.23.2", "v22.23.2"),
        ("2.100.0", "gh version 2.100.0 (2026-09-01)"),
        ("1228", "/home/runner/.cache/ms-playwright/chromium-1228"),
        ("9.1.1", "pytest 9.1.1"),
        ("1.29.1", "rustup 1.29.1 (a1b2c3d4e 2026-06-01)"),
    ],
)
def test_the_real_output_shapes_still_match_exactly(version: str, output: str) -> None:
    """Overshoot control: the suffix rule must not reject a version followed by
    the space, parenthesis or end of line every real verify prints."""
    [result] = _classify([_entry("t", version)], [_record("t", 0, output)])
    assert result.status == "OK", result


def test_every_manifest_pin_matches_a_plain_rendering_of_itself() -> None:
    """Overshoot control over the live manifest: each non-floor pin, printed the
    way a `--version` prints it, must classify OK, so no real pin (a content
    hash, a Playwright revision, a two-part prefix) is unmatchable."""
    entries = [e for e in load_manifest()["entries"] if rtv.default_match(e) != "min"]
    assert entries, "no exact/prefix entries in the manifest: the control tests nothing"
    records = [_record(e["name"], 0, f"{e['name']} {e['version']}\n") for e in entries]
    stripped = [{**e, "provision": "host"} for e in entries]
    bad = [r for r in _classify(stripped, records) if r.status != "OK"]
    assert not bad, bad


# ----- the probe itself, run for real against the installed executables -------------

PROBE_NEEDS = ("bash", "timeout", "base64", "head", "cat")


def _dirs_of(*tools: str) -> list[str]:
    """Real directories holding `tools`; a probe prerequisite missing here fails loud."""
    found = {tool: shutil.which(tool) for tool in tools}
    missing = sorted(tool for tool, path in found.items() if not path)
    assert not missing, f"probe prerequisites absent on this host: {missing}"
    return sorted({str(Path(path).parent) for path in found.values() if path})


def _probe_locally(
    entries: list[dict], runner_dir: Path, home: Path, login_path: str | None = None
) -> tuple[list, str]:
    """Run the verifier's own probe script under bash, as this user, no sudo."""
    proc = subprocess.run(
        ["bash", "-s"],
        input=rtv._probe_script(entries, str(runner_dir)),
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": login_path or os.environ["PATH"], "HOME": str(home)},
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


def _installed_version(tool: str) -> str:
    """The real toolchain's own reported version, or UNAVAILABLE."""
    if not shutil.which(tool):
        pytest.skip(f"UNAVAILABLE: no real {tool} installed here, so its probe cannot run")
    out = subprocess.run([tool, "--version"], capture_output=True, text=True, check=True).stdout
    found = re.match(rf"{tool} (\d+\.\d+\.\d+)\b", out)
    assert found, f"unexpected `{tool} --version` output: {out!r}"
    return found.group(1)


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
    cargo, rustc, entries = _rust_entries()
    installed = {"cargo": _installed_version("cargo"), "rustc": _installed_version("rustc")}
    pins = {tool: "0.0.1" if tool == moved else version for tool, version in installed.items()}
    pinned = [{**entries[0], "version": pins["cargo"]}, {**entries[1], "version": pins["rustc"]}]
    runner = _runner_with_path(tmp_path, _dirs_of(*PROBE_NEEDS, "cargo", "rustc"))
    results, _ = _probe_locally(pinned, runner, tmp_path)
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
    results, stdout = _probe_locally([_bash_entry()], runner, tmp_path)
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
    results, stdout = _probe_locally(entries, runner, tmp_path, login_path)
    assert f"{rtv.RECORD} PATHSRC {runner}" in stdout, stdout
    status = {r.name: (r.status, r.got) for r in results}
    assert status["bash"][0] == "OK", status
    assert status["elsewhere"][0] == "MISSING" and "PATH" in status["elsewhere"][1], status
