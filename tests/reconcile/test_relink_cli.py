"""Relink and reconcile CLI entrypoints.

Split from ``tests/reconcile/test_relink.py`` for the #1390 file-size gate.

[if] relink CLI defaults or risk gates regress [then] state.db mutates without consent, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.reconcile import __main__ as reconcile_cli
from apps.reconcile import relink
from tests.reconcile.relink_support import (
    _cli_data_dir,
    _make_state_db,
    _rows,
    _sha256,
)

pytestmark = pytest.mark.requirement("RECON-01")

# ----- CLI --------------------------------------------------------------


def test_cli_dry_run_is_the_default_and_writes_nothing(tmp_path: Path) -> None:
    data_dir, disk, _ = _cli_data_dir(tmp_path)
    db = data_dir / "state" / "state.db"
    digest = _sha256(db)
    code = relink.cmd_relink(
        ["--data-dir", str(data_dir), "--roots", str(disk), "--index-cache", str(tmp_path / "c.json")]
    )
    assert code == 0
    assert _sha256(db) == digest, "if a bare `relink` mutates state.db then dry-run is not the default"
    logs = list((data_dir / "reconcile").glob("relink-log-*"))
    assert len(logs) == 1 and logs[0].name.endswith(relink.DRY_RUN_LOG_SUFFIX)


def test_cli_live_without_the_risk_flag_refuses(tmp_path: Path) -> None:
    data_dir, disk, _ = _cli_data_dir(tmp_path)
    db = data_dir / "state" / "state.db"
    digest = _sha256(db)
    code = relink.cmd_relink(["--data-dir", str(data_dir), "--roots", str(disk), "--live"])
    assert code == 2
    assert _sha256(db) == digest
    assert not (data_dir / "reconcile").exists(), "if a refused live run still writes a log then the gate runs too late"


def test_cli_ambiguous_bucket_needs_the_risk_flag_even_dry(tmp_path: Path) -> None:
    data_dir, disk, _ = _cli_data_dir(tmp_path)
    code = relink.cmd_relink(
        [
            "--data-dir",
            str(data_dir),
            "--roots",
            str(disk),
            "--bucket",
            "relinkable-ambiguous",
        ]
    )
    assert code == 2, (
        "if the ambiguous bucket is selectable without the risk flag then the human-adjudication rule is only a comment"
    )


def test_cli_live_then_undo_round_trips(tmp_path: Path) -> None:
    data_dir, disk, target = _cli_data_dir(tmp_path)
    db = data_dir / "state" / "state.db"
    before = _rows(db)
    common = [
        "--data-dir",
        str(data_dir),
        "--roots",
        str(disk),
        "--index-cache",
        str(tmp_path / "c.json"),
    ]
    assert relink.cmd_relink([*common, "--live", "--i-understand-the-risks"]) == 0
    idx = relink.TRACK_COLUMNS.index("file_path")
    assert _rows(db)["a"][idx] == str(target)

    logs = [
        p for p in (data_dir / "reconcile").glob("relink-log-*.csv") if not p.name.endswith(relink.DRY_RUN_LOG_SUFFIX)
    ]
    assert len(logs) == 1
    assert (
        relink.cmd_relink_undo(
            [
                "--data-dir",
                str(data_dir),
                "--log",
                str(logs[0]),
                "--live",
                "--i-understand-the-risks",
            ]
        )
        == 0
    )
    after = _rows(db)
    for pos, col in enumerate(relink.TRACK_COLUMNS):
        if col == "updated_at":
            continue
        assert after["a"][pos] == before["a"][pos], (
            f"if the CLI round trip does not restore {col} then --live is not safely reversible from its log alone"
        )


def test_cli_undo_dry_run_reports_without_writing(tmp_path: Path) -> None:
    data_dir, disk, _target = _cli_data_dir(tmp_path)
    db = data_dir / "state" / "state.db"
    common = ["--data-dir", str(data_dir), "--roots", str(disk), "--index-cache", str(tmp_path / "c.json")]
    relink.cmd_relink([*common, "--live", "--i-understand-the-risks"])
    logs = [
        p for p in (data_dir / "reconcile").glob("relink-log-*.csv") if not p.name.endswith(relink.DRY_RUN_LOG_SUFFIX)
    ]
    digest = _sha256(db)
    code = relink.cmd_relink_undo(["--data-dir", str(data_dir), "--log", str(logs[0])])
    assert code == 0 and _sha256(db) == digest, "if `relink-undo` without --live mutates state.db then dry-run is a lie"


def test_cli_nothing_to_apply_exits_one(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    _make_state_db(
        data_dir / "state" / "state.db",
        [{"stable_id": "a", "file_path": "/gone/nothing-like-this.mp3"}],
    )
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    code = relink.cmd_relink(
        [
            "--data-dir",
            str(data_dir),
            "--roots",
            str(empty_root),
            "--index-cache",
            str(tmp_path / "c.json"),
        ]
    )
    assert code == 1


def test_dispatcher_lists_and_rejects_unknown_subcommands(capsys) -> None:
    assert reconcile_cli.main([]) == 0
    listed = capsys.readouterr().out
    for name in ("relink", "relink-undo", "relink-review"):
        assert name in listed
    assert reconcile_cli.main(["not-a-command"]) == 2


def test_dispatcher_forwards_argv_to_the_subcommand(tmp_path: Path) -> None:
    data_dir, disk, _ = _cli_data_dir(tmp_path)
    code = reconcile_cli.main(
        [
            "relink",
            "--data-dir",
            str(data_dir),
            "--roots",
            str(disk),
            "--index-cache",
            str(tmp_path / "c.json"),
        ]
    )
    assert code == 0


def test_dispatcher_refuses_arguments_for_legacy_no_arg_commands(capsys) -> None:
    assert reconcile_cli.main(["list-broken", "--nope"]) == 2
    assert "takes no arguments" in capsys.readouterr().err
