"""CAT-03 CLI golden tests for ``apps.pairings.{add,remove,list}``.

Exercises the three entrypoints end-to-end against a tmp_path state DB
(no live DB writes).
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from apps.pairings import add as cli_add
from apps.pairings import list as cli_list
from apps.pairings import remove as cli_remove


pytestmark = pytest.mark.requirement("CAT-03")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def test_add_writes_row(db_path: Path, capsys: pytest.CaptureFixture) -> None:
    rc = cli_add.main([
        "--db", str(db_path),
        "--from", "aaa111",
        "--to", "bbb222",
        "--direction", "into",
        "--notes", "peak transition",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "aaa111 -> bbb222" in out
    assert "[into]" in out


def test_add_bad_direction_returns_2(
    db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # argparse converts the bad choice into exit-2 itself; confirm behaviour.
    with pytest.raises(SystemExit) as excinfo:
        cli_add.main([
            "--db", str(db_path),
            "--from", "a", "--to", "b",
            "--direction", "garbage",
        ])
    assert excinfo.value.code == 2


def test_add_self_pair_returns_2(
    db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = cli_add.main([
        "--db", str(db_path),
        "--from", "x", "--to", "x", "--direction", "into",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "must differ" in err


def test_remove_roundtrip(
    db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    cli_add.main([
        "--db", str(db_path),
        "--from", "a", "--to", "b", "--direction", "into",
    ])
    capsys.readouterr()  # discard add output
    rc = cli_remove.main([
        "--db", str(db_path),
        "--from", "a", "--to", "b", "--direction", "into",
    ])
    assert rc == 0


def test_remove_missing_returns_1(
    db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = cli_remove.main([
        "--db", str(db_path),
        "--from", "a", "--to", "b", "--direction", "into",
    ])
    assert rc == 1


def test_list_json_format(db_path: Path) -> None:
    cli_add.main([
        "--db", str(db_path),
        "--from", "a1", "--to", "b1", "--direction", "into",
    ])
    cli_add.main([
        "--db", str(db_path),
        "--from", "a1", "--to", "c1", "--direction", "either",
    ])
    buf = io.StringIO()
    rc = cli_list.main(["--db", str(db_path), "--format", "json"], out=buf)
    assert rc == 0
    data = json.loads(buf.getvalue())
    assert isinstance(data, list)
    assert len(data) == 2
    # Entries preserve direction and source tags.
    directions = {row["direction"] for row in data}
    assert directions == {"into", "either"}


def test_list_csv_format(db_path: Path) -> None:
    cli_add.main([
        "--db", str(db_path),
        "--from", "a", "--to", "b", "--direction", "into",
    ])
    buf = io.StringIO()
    cli_list.main(["--db", str(db_path), "--format", "csv"], out=buf)
    text = buf.getvalue()
    # Header + at least one data row.
    lines = [line for line in text.splitlines() if line]
    assert lines[0].startswith("from_stable_id")
    assert "a,b,into" in lines[1]


def test_list_filter_by_from(db_path: Path) -> None:
    cli_add.main([
        "--db", str(db_path),
        "--from", "alpha", "--to", "beta", "--direction", "into",
    ])
    cli_add.main([
        "--db", str(db_path),
        "--from", "gamma", "--to", "delta", "--direction", "into",
    ])
    buf = io.StringIO()
    cli_list.main(
        ["--db", str(db_path), "--from", "alpha", "--format", "json"],
        out=buf,
    )
    data = json.loads(buf.getvalue())
    assert len(data) == 1 and data[0]["from_stable_id"] == "alpha"


def test_three_add_one_remove_one_list(db_path: Path) -> None:
    # Spec test from 08-01-PLAN §3: three add + one remove + one list.
    cli_add.main(["--db", str(db_path), "--from", "a", "--to", "b",
                  "--direction", "into"])
    cli_add.main(["--db", str(db_path), "--from", "a", "--to", "c",
                  "--direction", "into"])
    cli_add.main(["--db", str(db_path), "--from", "b", "--to", "c",
                  "--direction", "either"])
    cli_remove.main(["--db", str(db_path), "--from", "a", "--to", "c",
                     "--direction", "into"])
    buf = io.StringIO()
    cli_list.main(["--db", str(db_path), "--format", "json"], out=buf)
    data = json.loads(buf.getvalue())
    pairs = {(r["from_stable_id"], r["to_stable_id"], r["direction"])
             for r in data}
    assert pairs == {("a", "b", "into"), ("b", "c", "either")}
