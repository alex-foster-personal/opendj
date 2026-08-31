"""Phase 4 SYNC-04: apply_cues CLI tests (dry-run + safety gating + rails).

Covers:
  * dry-run + flag-gating (existing tests)
  * six-rail safety harness for the stub live path:
      - rail 1 (pgrep gate)
      - rail 2 (timestamped backup)
      - rail 4 (verifier wired into LiveWriteSession)
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from apps.sync.apply_cues import dry_run, live_run, main
from apps.sync.safety import SafetyAbort

# Live-write MECHANICS against tmp fixtures: runs with the one-way rekordbox
# import gate ON (root conftest reads the marker). Never a real rb target.
pytestmark = [pytest.mark.requirement("SYNC-04"), pytest.mark.rekordbox_writeback]


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


def test_bulk_all_tracks_without_only_tracks_flag_permitted(tmp_path: Path):
    """Regression for adversarial #4 (HIGH): the previous gate refused
    ``--bulk`` without ``--tracks``, which blocked the legitimate
    bulk-all-tracks workflow. ``--bulk`` paired with
    ``--i-understand-the-risks`` and no ``--tracks`` MUST proceed (empty
    diff exits 0; the ``--i-understand-the-risks`` requirement is the
    real safety gate and is enforced in main()).
    """
    path = _make_diff(tmp_path, [])
    rc = main(
        [
            "--diff-csv", str(path),
            "--live",
            "--bulk",
            "--i-understand-the-risks",
        ]
    )
    assert rc != 2, "bulk-all-tracks must not be blocked by the --tracks gate"
    assert rc == 0


def test_main_missing_csv_dry_run(tmp_path: Path, capsys):
    assert main(["--diff-csv", str(tmp_path / "nope.csv")]) == 0
    assert "no rows" in capsys.readouterr().out


def test_main_prefer_flag_accepted(tmp_path: Path):
    path = _make_diff(tmp_path, [])
    assert main(["--diff-csv", str(path), "--prefer", "rb"]) == 0


def test_main_prune_flag_accepted(tmp_path: Path):
    path = _make_diff(tmp_path, [])
    assert main(["--diff-csv", str(path), "--prune"]) == 0


# ---------------------------------------------------- six-rail live tests


def _cue_rows() -> list[dict]:
    return [
        {
            "rb_content_id": "1",
            "djay_uuid": "a",
            "rb_cue_count": "1",
            "djay_cue_count": "1",
            "rb_only_positions": "500",
            "djay_only_positions": "",
            "conflicting_positions": "",
            "union_count": "1",
        }
    ]


class TestCuesLiveRails:
    def test_pgrep_aborts_stub(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        rb_db = tmp_path / "master.db"
        rb_db.write_bytes(b"rb")
        djay_db = tmp_path / "ml.db"
        djay_db.write_bytes(b"dj")
        monkeypatch.setattr(
            "apps.sync.safety._is_running",
            lambda name: name == "Rekordbox",
        )
        with pytest.raises(SafetyAbort, match="Rekordbox is running"):
            live_run(
                _cue_rows(),
                flag_ok=True,
                cautious=True,
                rb_db_path=rb_db,
                djay_db_path=djay_db,
            )

    def test_backup_created_before_stub_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
    ) -> None:
        rb_db = tmp_path / "master.db"
        rb_db.write_bytes(b"rb-contents")
        djay_db = tmp_path / "ml.db"
        djay_db.write_bytes(b"dj")
        monkeypatch.setattr(
            "apps.sync.safety._is_running", lambda _n: False,
        )
        rc = live_run(
            _cue_rows(),
            flag_ok=True,
            cautious=True,
            rb_db_path=rb_db,
            djay_db_path=djay_db,
        )
        assert rc == 0
        backups = list(rb_db.parent.glob("master.db.bak.*"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == b"rb-contents"

    def test_stub_verifier_path_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rail 4 wiring: the stub verifier is invoked for each row.

        We assert the reversal shell script received a snippet (which
        only happens when ``verify_readback`` returned True inside
        live_run).
        """
        rb_db = tmp_path / "master.db"
        rb_db.write_bytes(b"rb")
        djay_db = tmp_path / "ml.db"
        djay_db.write_bytes(b"dj")
        monkeypatch.setattr(
            "apps.sync.safety._is_running", lambda _n: False,
        )
        rc = live_run(
            _cue_rows(),
            flag_ok=True,
            cautious=True,
            rb_db_path=rb_db,
            djay_db_path=djay_db,
        )
        assert rc == 0
        scripts = list(
            (rb_db.parent.parent).rglob("reverse.sh"),
        )
        # The session's reversal script lives under
        # <data>/sync/reversal/<ts>/reverse.sh. When we point db_path at
        # tmp_path/master.db the default reversal_root resolves to
        # tmp_path.parent/sync/reversal. Walk broadly to find it.
        assert any(
            "revert RB cues" in p.read_text() for p in scripts
        ) or any(
            "revert RB cues" in p.read_text()
            for p in tmp_path.rglob("reverse.sh")
        )



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

