"""scripts/runner_toolset_verify.py classifies a host truthfully.

The probe itself needs a real runner host (it is run by hand, and by the host
audit, against agentbox); these tests pin the classification and exit-code
contract the host audit depends on, using the probe's own record format.

Regression lines:
  - if a verify that prints another version reports OK then broken
  - if a verify that exits nonzero reports anything but MISSING then broken
  - if a provided executable absent from the runner PATH reports OK then broken
  - if a probe that never ran (ssh or sudo failure) reports OK or MISSING then broken
  - if one UNKNOWN among OKs exits 0, or one MISSING exits other than 1, then broken
"""

from __future__ import annotations

import base64

import pytest

from scripts import runner_toolset_verify as rtv


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
