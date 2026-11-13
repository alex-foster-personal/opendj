"""``opendj sync ratings`` parity with ``python -m apps.sync apply-ratings`` (issue #12976, PLATFORM-12976).

[if] an agent asks opendj for ratings sync help [then] the module CLI flags are listed, [else stop].
[if] an agent dry-runs a ratings diff CSV [then] resolution counts print and nothing is written, [else stop].
[if] an agent asks for a live run without acknowledgement [then] it is refused before any write, [else stop].
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED
from apps.opendj_cli.__main__ import main
from apps.sync import apply_ratings

pytestmark = pytest.mark.requirement("PLATFORM-12976")


def _diff_csv(tmp_path: Path) -> Path:
    """Write a three-row ratings diff CSV with two accept_rb rows and one conflict."""
    path = tmp_path / "ratings-diff.csv"
    rows = [
        {"rb_content_id": "11", "djay_uuid": "u1", "rb_value": "4", "djay_value": "0", "resolution": "accept_rb"},
        {"rb_content_id": "12", "djay_uuid": "u2", "rb_value": "5", "djay_value": "0", "resolution": "accept_rb"},
        {"rb_content_id": "13", "djay_uuid": "u3", "rb_value": "2", "djay_value": "3", "resolution": "conflict"},
    ]
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _forbid_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any call into the live write path fail the test."""

    def _boom(*_args: object, **_kwargs: object) -> int:
        """Stand-in for live_run that must never be reached."""
        raise AssertionError("live_run reached")

    monkeypatch.setattr(apply_ratings, "live_run", _boom)


def test_sync_ratings_help_lists_module_flags(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] opendj sync ratings --help runs [then] exit 0 lists the apply-ratings flags, [else stop]."""
    assert main(["sync", "ratings", "--help"]) == EXIT_CONFIRMED
    out = capsys.readouterr().out
    for flag in ("--diff-csv", "--live", "--bulk", "--tracks", "--prefer", "--i-understand-the-risks"):
        assert flag in out


def test_sync_help_names_ratings(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] opendj sync --help runs [then] the ratings subcommand is named, [else stop]."""
    assert main(["sync", "--help"]) == EXIT_CONFIRMED
    assert "ratings" in capsys.readouterr().out


def test_sync_ratings_dry_run_prints_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a dry run reads a diff CSV [then] resolution counts print and no write runs, [else stop]."""
    _forbid_writes(monkeypatch)
    assert main(["sync", "ratings", "--diff-csv", str(_diff_csv(tmp_path))]) == EXIT_CONFIRMED
    out = capsys.readouterr().out
    assert "dry-run summary" in out
    assert "accept_rb: 2" in out
    assert "conflict: 1" in out


def test_sync_ratings_dry_run_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a dry run runs with --json [then] the plan is one JSON document, [else stop]."""
    _forbid_writes(monkeypatch)
    path = _diff_csv(tmp_path)
    assert main(["--json", "sync", "ratings", "--diff-csv", str(path)]) == EXIT_CONFIRMED
    document = json.loads(capsys.readouterr().out)
    assert document == {
        "diff_csv": str(path),
        "dry_run": True,
        "resolutions": {"accept_rb": 2, "conflict": 1},
        "rows": 3,
    }


def test_sync_ratings_live_without_ack_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] --live runs without --i-understand-the-risks [then] exit 1 refuses before the module, [else stop]."""

    def _module_main(_argv: list[str] | None = None) -> int:
        """Stand-in for apply_ratings.main that must never be reached."""
        raise AssertionError("apply_ratings.main reached")

    monkeypatch.setattr(apply_ratings, "main", _module_main)
    _forbid_writes(monkeypatch)
    code = main(["--json", "sync", "ratings", "--live", "--diff-csv", str(_diff_csv(tmp_path))])
    assert code == EXIT_FAILED
    document = json.loads(capsys.readouterr().out)
    assert document["error"]["code"] == "acknowledgement_required"
    assert "--i-understand-the-risks" in document["error"]["message"]


def test_sync_ratings_live_with_ack_forwards_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] --live runs with the acknowledgement [then] the module gets the same flags, [else stop]."""
    seen: list[list[str]] = []

    def _module_main(argv: list[str] | None = None) -> int:
        """Record the forwarded argv instead of writing anything."""
        seen.append(list(argv or []))
        return 0

    monkeypatch.setattr(apply_ratings, "main", _module_main)
    path = _diff_csv(tmp_path)
    tokens = ["sync", "ratings", "--diff-csv", str(path), "--live", "--tracks", "11,u2", "--i-understand-the-risks"]
    assert main(tokens) == EXIT_CONFIRMED
    assert seen == [
        ["--diff-csv", str(path), "--prefer", "newest", "--live", "--tracks", "11,u2", "--i-understand-the-risks"]
    ]


def test_sync_unknown_subcommand_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """[if] opendj sync gets an unknown subcommand [then] exit 1 with a usage refusal, [else stop]."""
    assert main(["--json", "sync", "cues"]) == EXIT_FAILED
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"
