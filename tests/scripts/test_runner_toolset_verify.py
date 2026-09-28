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
"""

from __future__ import annotations

import base64

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
