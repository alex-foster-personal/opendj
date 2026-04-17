"""Phase 4 SYNC-04: apply_cues CLI tests (dry-run + safety gating)."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from apps.sync.apply_cues import dry_run, main

pytestmark = pytest.mark.requirement("SYNC-04")


def _make_diff(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "cue-diff.csv"
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "rb_content_id",
                "djay_uuid",
                "rb_cue_count",
                "djay_cue_count",
                "rb_only_positions",
                "djay_only_positions",
                "conflicting_positions",
                "union_count",
            ],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return path


def test_dry_run_empty(capsys):
    assert dry_run([]) == 0
    assert "no rows" in capsys.readouterr().out


def test_dry_run_counts_categories(capsys):
    rows = [
        {"rb_only_positions": "500", "djay_only_positions": "", "conflicting_positions": ""},
        {"rb_only_positions": "", "djay_only_positions": "2000", "conflicting_positions": ""},
        {"rb_only_positions": "", "djay_only_positions": "", "conflicting_positions": "1500"},
    ]
    dry_run(rows)
    out = capsys.readouterr().out
    assert "add to RB" in out
    assert "add to djay" in out
    assert "conflicts:" in out


def test_main_default_is_dry_run(tmp_path: Path):
    path = _make_diff(tmp_path, [])
    assert main(["--diff-csv", str(path)]) == 0


def test_main_live_requires_confirm(tmp_path: Path):
    path = _make_diff(tmp_path, [])
    # --live --cautious without --i-understand-the-risks aborts.
    assert main(["--diff-csv", str(path), "--live", "--cautious"]) == 2


def test_main_live_without_mode_aborts(tmp_path: Path):
    path = _make_diff(tmp_path, [])
    # --live without --cautious or --bulk aborts.
    rc = main(
        [
            "--diff-csv", str(path),
            "--live",
            "--i-understand-the-risks",
        ]
    )
    assert rc == 2


def test_main_bulk_without_cautious_aborts(tmp_path: Path):
    path = _make_diff(tmp_path, [])
    rc = main(
        [
            "--diff-csv", str(path),
            "--live",
            "--bulk",
            "--i-understand-the-risks",
        ]
    )
    # bulk without tracks set (no prior cautious) -> aborts with 2
    assert rc == 2


def test_main_missing_csv_dry_run(tmp_path: Path, capsys):
    assert main(["--diff-csv", str(tmp_path / "nope.csv")]) == 0
    assert "no rows" in capsys.readouterr().out


def test_main_prefer_flag_accepted(tmp_path: Path):
    path = _make_diff(tmp_path, [])
    assert main(["--diff-csv", str(path), "--prefer", "rb"]) == 0


def test_main_prune_flag_accepted(tmp_path: Path):
    path = _make_diff(tmp_path, [])
    assert main(["--diff-csv", str(path), "--prune"]) == 0


def test_live_cautious_emits_stub_banner(tmp_path: Path, capsys):
    """Regression for [I1]: the live-cautious CLI path must print a loud
    'STUB LIVE MODE -- no real cue writes' banner to stderr so operators
    cannot mistake a safety-rail-only pass for an actual cue write.

    We exercise ``live_run`` directly with an empty rows list so the
    banner fires before any LiveWriteSession rail would run. This keeps
    the test sandbox-safe (no pgrep, no backup copy) while still asserting
    the banner emission contract.
    """
    from apps.sync import apply_cues as mod

    rc = mod.live_run(
        rows=[],
        flag_ok=True,
        cautious=True,
        bulk=False,
        rb_db_path=tmp_path / "rb.db",
        djay_db_path=tmp_path / "djay.db",
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "STUB LIVE MODE" in captured.err
    assert "NO REAL CUE WRITES" in captured.err
    assert "phase-04-probe-o2-runbook.md" in captured.err

