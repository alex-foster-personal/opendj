"""secscan trufflehog-summary: value suppressions drop UNVERIFIED placeholders only.

ADR-NEW-security-scans-on-self-hosted-pool. The weekly scan read UNKNOWN on every run
because placeholder URIs (an open-redirect test vector, a `ghs_redacted` stand-in) sat
in test files AND commit messages, where a path suppression cannot reach.

- if an unverified candidate's Raw matches a suppression then it is not counted
- if a VERIFIED candidate's Raw matches a suppression then it is still counted (a live
  secret is never filtered, whatever it looks like)
- if an unverified candidate matches no suppression then it is still counted
- if no suppression file is given then every candidate is counted (old behavior)
- if a suppression regex is anchored then a value that merely contains it is kept
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SECSCAN = Path(__file__).resolve().parents[2] / "scripts" / "security" / "secscan.py"

REDIRECT = "http://opendj.localhost:7700@attacker.example"
SUPPRESSIONS = [r"^https?://opendj\.localhost:[0-9]+@attacker\.example$"]


def _record(raw: str, *, verified: bool, file: str = "tests/x.py") -> dict:
    return {
        "DetectorName": "URI",
        "Verified": verified,
        "Raw": raw,
        "SourceMetadata": {"Data": {"Git": {"file": file, "line": 1, "commit": "a" * 40}}},
    }


def _summarize(tmp_path: Path, records: list[dict], suppressions: list[str] | None) -> tuple[int, int, str]:
    jsonl = tmp_path / "history.jsonl"
    jsonl.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    count = tmp_path / "history.count"
    cmd = [sys.executable, str(SECSCAN), "trufflehog-summary", str(jsonl), "--count-file", str(count)]
    if suppressions is not None:
        sup = tmp_path / "suppress.txt"
        sup.write_text("\n".join(suppressions) + "\n", encoding="utf-8")
        cmd += ["--suppress-raw", str(sup)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, int(count.read_text(encoding="utf-8").strip()), proc.stdout


def test_unverified_placeholder_is_suppressed(tmp_path: Path) -> None:
    rc, count, out = _summarize(tmp_path, [_record(REDIRECT, verified=False)], SUPPRESSIONS)
    assert (rc, count) == (0, 0), out


def test_verified_hit_is_never_suppressed(tmp_path: Path) -> None:
    rc, count, out = _summarize(tmp_path, [_record(REDIRECT, verified=True)], SUPPRESSIONS)
    assert (rc, count) == (0, 1), out
    assert "URI verified" in out


def test_unmatched_unverified_candidate_is_kept(tmp_path: Path) -> None:
    other = "postgres://admin:hunter2@db.example.com:5432/app"
    rc, count, out = _summarize(tmp_path, [_record(other, verified=False)], SUPPRESSIONS)
    assert (rc, count) == (0, 1), out


def test_anchored_regex_keeps_a_longer_value(tmp_path: Path) -> None:
    longer = REDIRECT + ".real-host.com"
    rc, count, out = _summarize(tmp_path, [_record(longer, verified=False)], SUPPRESSIONS)
    assert (rc, count) == (0, 1), out


def test_without_suppressions_everything_counts(tmp_path: Path) -> None:
    records = [_record(REDIRECT, verified=False), _record(REDIRECT, verified=True)]
    rc, count, out = _summarize(tmp_path, records, None)
    assert (rc, count) == (0, 2), out
